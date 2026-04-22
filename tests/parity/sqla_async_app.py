"""Shared app factory for ASYNC SQLAlchemy driver (asyncpg).

Mirrors the sync app, but uses AsyncEngine / AsyncSession / await.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy import (
    and_,
    between,
    delete,
    func,
    insert,
    not_,
    or_,
    select,
    text,
    update,
    union,
    intersect,
)
from sqlalchemy.exc import (
    IntegrityError,
    MultipleResultsFound,
    NoResultFound,
    PendingRollbackError,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    contains_eager,
    joinedload,
    selectinload,
    subqueryload,
)

from tests.parity.sqla_common import (
    Base,
    Category,
    CategoryIn,
    CategoryOut,
    Item,
    ItemIn,
    ItemOut,
    ItemWithOwnerOut,
    OrderLine,
    StatusEnum,
    TagArr,
    User,
    UserIn,
    UserOut,
    IS_SQLITE,
)


def build_app(db_url: str) -> FastAPI:
    if IS_SQLITE:
        engine = create_async_engine(db_url)
    else:
        engine = create_async_engine(
            db_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=300,
        )
    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    # Create all tables (run_sync handles the metadata creation)
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_init()) if False else None
    # We'll init via startup event so event loop is safe

    app = FastAPI()

    @app.on_event("startup")
    async def _startup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    async def get_db() -> AsyncIterator[AsyncSession]:
        async with AsyncSessionLocal() as s:
            yield s

    @app.get("/health")
    async def health():
        return {"ok": True}

    # === Users ==========================================================
    @app.post("/users", response_model=UserOut)
    async def create_user(payload: UserIn, db: AsyncSession = Depends(get_db)):
        u = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(u)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "dup")
        await db.refresh(u)
        return u

    @app.get("/users/{uid}", response_model=UserOut)
    async def get_user(uid: int, db: AsyncSession = Depends(get_db)):
        u = await db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        return u

    @app.get("/users", response_model=list[UserOut])
    async def list_users(
        limit: int = 50, offset: int = 0, order: str = "id",
        db: AsyncSession = Depends(get_db),
    ):
        col = getattr(User, order, User.id)
        stmt = select(User).order_by(col).limit(limit).offset(offset)
        rs = await db.scalars(stmt)
        return list(rs.all())

    @app.put("/users/{uid}", response_model=UserOut)
    async def update_user(uid: int, payload: UserIn, db: AsyncSession = Depends(get_db)):
        u = await db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        u.email = payload.email
        u.name = payload.name
        u.age = payload.age
        await db.commit()
        await db.refresh(u)
        return u

    @app.delete("/users/{uid}")
    async def delete_user(uid: int, db: AsyncSession = Depends(get_db)):
        u = await db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        await db.delete(u)
        await db.commit()
        return {"deleted": uid}

    @app.get("/users/by-email/{email}", response_model=UserOut)
    async def user_by_email(email: str, db: AsyncSession = Depends(get_db)):
        stmt = select(User).where(User.email == email)
        rs = await db.scalars(stmt)
        try:
            return rs.one()
        except NoResultFound:
            raise HTTPException(404, "not found")

    @app.get("/users/{uid}/or-none")
    async def user_or_none(uid: int, db: AsyncSession = Depends(get_db)):
        stmt = select(User).where(User.id == uid)
        rs = await db.scalars(stmt)
        u = rs.one_or_none()
        return {"found": u is not None, "id": u.id if u else None}

    @app.get("/q/users-first")
    async def first_user(db: AsyncSession = Depends(get_db)):
        rs = await db.scalars(select(User).order_by(User.id))
        u = rs.first()
        return {"id": u.id if u else None}

    # === Categories =====================================================
    @app.post("/categories", response_model=CategoryOut)
    async def create_cat(payload: CategoryIn, db: AsyncSession = Depends(get_db)):
        c = Category(name=payload.name)
        db.add(c)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "dup")
        await db.refresh(c)
        return c

    @app.get("/categories", response_model=list[CategoryOut])
    async def list_categories(db: AsyncSession = Depends(get_db)):
        rs = await db.scalars(select(Category).order_by(Category.id))
        return list(rs.all())

    # === Items ==========================================================
    @app.post("/items", response_model=ItemOut)
    async def create_item(payload: ItemIn, db: AsyncSession = Depends(get_db)):
        owner = await db.get(User, payload.owner_id)
        if not owner:
            raise HTTPException(400, "owner missing")
        it = Item(**payload.model_dump())
        db.add(it)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "integrity")
        await db.refresh(it)
        return it

    @app.get("/items/{iid}", response_model=ItemOut)
    async def get_item(iid: int, db: AsyncSession = Depends(get_db)):
        it = await db.get(Item, iid)
        if not it:
            raise HTTPException(404, "not found")
        return it

    @app.get("/items/{iid}/with-owner")
    async def item_with_owner(iid: int, db: AsyncSession = Depends(get_db)):
        stmt = select(Item).options(joinedload(Item.owner)).where(Item.id == iid)
        rs = await db.scalars(stmt)
        it = rs.unique().one_or_none()
        if not it:
            raise HTTPException(404, "not found")
        return {"id": it.id, "title": it.title, "owner_name": it.owner.name}

    @app.get("/q/items-by-owner/{uid}", response_model=list[ItemOut])
    async def items_by_owner(uid: int, db: AsyncSession = Depends(get_db)):
        stmt = select(Item).where(Item.owner_id == uid).order_by(Item.id)
        rs = await db.scalars(stmt)
        return list(rs.all())

    @app.get("/q/items-by-owner-selectin/{uid}")
    async def items_by_owner_selectin(uid: int, db: AsyncSession = Depends(get_db)):
        stmt = (
            select(User).options(selectinload(User.items)).where(User.id == uid)
        )
        rs = await db.scalars(stmt)
        u = rs.one_or_none()
        if not u:
            raise HTTPException(404, "not found")
        return {"owner": u.name, "count": len(u.items), "ids": sorted([i.id for i in u.items])}

    @app.get("/q/items-by-owner-joinedload/{uid}")
    async def items_by_owner_joined(uid: int, db: AsyncSession = Depends(get_db)):
        stmt = select(User).options(joinedload(User.items)).where(User.id == uid)
        rs = await db.scalars(stmt)
        u = rs.unique().one_or_none()
        if not u:
            raise HTTPException(404, "not found")
        return {"count": len(u.items)}

    # === Filters ========================================================
    @app.get("/q/items-filter")
    async def filter_items(
        min_price: float = 0.0,
        max_price: float = 1e12,
        status: Optional[StatusEnum] = None,
        title_like: Optional[str] = None,
        db: AsyncSession = Depends(get_db),
    ):
        stmt = select(Item).where(between(Item.price, min_price, max_price))
        if status is not None:
            stmt = stmt.where(Item.status == status)
        if title_like:
            stmt = stmt.where(Item.title.like(f"%{title_like}%"))
        stmt = stmt.order_by(Item.id)
        rs = await db.scalars(stmt)
        out = [{"id": i.id, "title": i.title, "price": i.price} for i in rs.all()]
        return {"count": len(out), "items": out}

    @app.get("/q/items-filter-ilike")
    async def filter_ilike(pat: str, db: AsyncSession = Depends(get_db)):
        stmt = select(Item).where(Item.title.ilike(f"%{pat}%")).order_by(Item.id)
        rs = await db.scalars(stmt)
        return [{"id": i.id, "title": i.title} for i in rs.all()]

    @app.get("/q/items-filter-in")
    async def filter_in(ids: str = "", db: AsyncSession = Depends(get_db)):
        id_list = [int(x) for x in ids.split(",") if x]
        if not id_list:
            return []
        stmt = select(Item).where(Item.id.in_(id_list)).order_by(Item.id)
        rs = await db.scalars(stmt)
        return [{"id": i.id} for i in rs.all()]

    @app.get("/q/items-filter-and")
    async def filter_and(min_q: int = 0, max_p: float = 1e12, db: AsyncSession = Depends(get_db)):
        stmt = select(Item).where(and_(Item.quantity >= min_q, Item.price <= max_p)).order_by(Item.id)
        rs = await db.scalars(stmt)
        return [{"id": i.id} for i in rs.all()]

    @app.get("/q/items-filter-or")
    async def filter_or(q: int = 0, p: float = 0, db: AsyncSession = Depends(get_db)):
        stmt = select(Item).where(or_(Item.quantity == q, Item.price == p)).order_by(Item.id)
        rs = await db.scalars(stmt)
        return [{"id": i.id} for i in rs.all()]

    @app.get("/q/items-filter-not")
    async def filter_not(status: StatusEnum = StatusEnum.draft, db: AsyncSession = Depends(get_db)):
        stmt = select(Item).where(not_(Item.status == status)).order_by(Item.id)
        rs = await db.scalars(stmt)
        return [{"id": i.id} for i in rs.all()]

    # === Aggregates =====================================================
    @app.get("/q/items-stats")
    async def stats(db: AsyncSession = Depends(get_db)):
        result = await db.execute(
            select(
                func.count(Item.id),
                func.coalesce(func.sum(Item.price), 0),
                func.coalesce(func.avg(Item.price), 0),
                func.coalesce(func.max(Item.price), 0),
                func.coalesce(func.min(Item.price), 0),
            )
        )
        row = result.one()
        return {
            "count": int(row[0]),
            "sum": float(row[1]),
            "avg": float(row[2]),
            "max": float(row[3]),
            "min": float(row[4]),
        }

    @app.get("/q/items-stats-by-status")
    async def stats_by_status(db: AsyncSession = Depends(get_db)):
        stmt = (
            select(Item.status, func.count(Item.id))
            .group_by(Item.status)
            .order_by(Item.status)
        )
        rs = await db.execute(stmt)
        return [{"status": r[0].value if hasattr(r[0], "value") else r[0], "count": int(r[1])} for r in rs.all()]

    @app.get("/q/items-having")
    async def having_test(min_count: int = 1, db: AsyncSession = Depends(get_db)):
        stmt = (
            select(Item.owner_id, func.count(Item.id))
            .group_by(Item.owner_id)
            .having(func.count(Item.id) >= min_count)
            .order_by(Item.owner_id)
        )
        rs = await db.execute(stmt)
        return [{"owner_id": r[0], "count": int(r[1])} for r in rs.all()]

    # === Joins ==========================================================
    @app.get("/q/items-join-owner")
    async def join_owner(db: AsyncSession = Depends(get_db)):
        stmt = (
            select(Item.id, User.name)
            .join(User, Item.owner_id == User.id)
            .order_by(Item.id)
        )
        rs = await db.execute(stmt)
        return [{"item_id": r[0], "owner_name": r[1]} for r in rs.all()]

    @app.get("/q/items-left-join-category")
    async def left_join_cat(db: AsyncSession = Depends(get_db)):
        stmt = (
            select(Item.id, Category.name)
            .join(Category, Item.category_id == Category.id, isouter=True)
            .order_by(Item.id)
        )
        rs = await db.execute(stmt)
        return [{"item_id": r[0], "category": r[1]} for r in rs.all()]

    # === Subqueries / CTEs / Window =====================================
    @app.get("/q/items-subquery-max")
    async def sub_max(db: AsyncSession = Depends(get_db)):
        sub = (
            select(Item.owner_id, func.max(Item.price).label("mx"))
            .group_by(Item.owner_id)
            .subquery()
        )
        stmt = (
            select(Item.id, Item.price, sub.c.mx)
            .join(sub, sub.c.owner_id == Item.owner_id)
            .order_by(Item.id)
        )
        rs = await db.execute(stmt)
        return [{"id": r[0], "price": float(r[1]), "max": float(r[2])} for r in rs.all()]

    @app.get("/q/items-cte-max")
    async def cte_max(db: AsyncSession = Depends(get_db)):
        cte = (
            select(Item.owner_id, func.max(Item.price).label("mx"))
            .group_by(Item.owner_id)
            .cte("mx")
        )
        stmt = select(cte.c.owner_id, cte.c.mx).order_by(cte.c.owner_id)
        rs = await db.execute(stmt)
        return [{"owner_id": r[0], "max": float(r[1])} for r in rs.all()]

    @app.get("/q/items-window")
    async def window(db: AsyncSession = Depends(get_db)):
        stmt = select(
            Item.id,
            Item.owner_id,
            Item.price,
            func.row_number().over(partition_by=Item.owner_id, order_by=Item.price.desc()).label("rn"),
        ).order_by(Item.id)
        rs = await db.execute(stmt)
        return [{"id": r[0], "owner": r[1], "price": float(r[2]), "rn": int(r[3])} for r in rs.all()]

    # === Raw SQL ========================================================
    @app.get("/raw/count-users")
    async def raw_count(db: AsyncSession = Depends(get_db)):
        rs = await db.execute(text(f"SELECT COUNT(*) AS n FROM {User.__tablename__}"))
        return {"n": int(rs.one()[0])}

    @app.get("/raw/user-name/{uid}")
    async def raw_user_name(uid: int, db: AsyncSession = Depends(get_db)):
        rs = await db.execute(
            text(f"SELECT name FROM {User.__tablename__} WHERE id = :id"), {"id": uid}
        )
        row = rs.one_or_none()
        return {"name": row[0] if row else None}

    # === Set operations =================================================
    @app.get("/q/items-union")
    async def union_test(db: AsyncSession = Depends(get_db)):
        a = select(Item.id).where(Item.status == StatusEnum.draft)
        b = select(Item.id).where(Item.status == StatusEnum.active)
        stmt = union(a, b).order_by("id")
        rs = await db.execute(stmt)
        return [{"id": r[0]} for r in rs.all()]

    @app.get("/q/items-intersect")
    async def intersect_test(db: AsyncSession = Depends(get_db)):
        a = select(Item.id).where(Item.price >= 0)
        b = select(Item.id).where(Item.quantity >= 0)
        stmt = intersect(a, b).order_by("id")
        rs = await db.execute(stmt)
        return [{"id": r[0]} for r in rs.all()]

    # === Bulk ===========================================================
    @app.post("/bulk/items")
    async def bulk_items(payload: list[ItemIn], db: AsyncSession = Depends(get_db)):
        objs = [Item(**p.model_dump()) for p in payload]
        db.add_all(objs)
        await db.commit()
        return {"inserted": len(objs)}

    @app.post("/bulk/insert-core")
    async def bulk_insert_core(payload: list[ItemIn], db: AsyncSession = Depends(get_db)):
        stmt = insert(Item).values([p.model_dump() for p in payload])
        result = await db.execute(stmt)
        await db.commit()
        return {"rowcount": result.rowcount}

    @app.post("/bulk/update-core")
    async def bulk_update(owner_id: int, new_price: float, db: AsyncSession = Depends(get_db)):
        stmt = update(Item).where(Item.owner_id == owner_id).values(price=new_price)
        result = await db.execute(stmt)
        await db.commit()
        return {"rowcount": result.rowcount}

    @app.post("/bulk/delete-core")
    async def bulk_delete(owner_id: int, db: AsyncSession = Depends(get_db)):
        stmt = delete(Item).where(Item.owner_id == owner_id)
        result = await db.execute(stmt)
        await db.commit()
        return {"rowcount": result.rowcount}

    @app.post("/returning/insert-item")
    async def insert_returning(payload: ItemIn, db: AsyncSession = Depends(get_db)):
        stmt = insert(Item).values(**payload.model_dump()).returning(Item.id, Item.title)
        rs = await db.execute(stmt)
        row = rs.one()
        await db.commit()
        return {"id": row[0], "title": row[1]}

    # === Session lifecycle =============================================
    @app.post("/session/flush-no-commit")
    async def flush_nocommit(payload: UserIn, db: AsyncSession = Depends(get_db)):
        u = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(u)
        await db.flush()
        uid = u.id
        await db.rollback()
        check = await db.get(User, uid)
        return {"assigned_id_truthy": bool(uid), "post_rollback_found": check is not None}

    @app.post("/session/savepoint")
    async def savepoint_test(payload: UserIn, db: AsyncSession = Depends(get_db)):
        outer_user = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(outer_user)
        await db.flush()
        async with db.begin_nested():
            inner = User(email=f"inner-{payload.email}", name="inner", age=1)
            db.add(inner)
            await db.flush()
            inner_id = inner.id
        await db.commit()
        return {"outer_id": outer_user.id, "inner_id_was": inner_id}

    @app.post("/session/savepoint-rollback")
    async def savepoint_rollback(payload: UserIn, db: AsyncSession = Depends(get_db)):
        outer_user = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(outer_user)
        await db.flush()
        try:
            async with db.begin_nested():
                inner = User(email=payload.email, name="dup", age=2)
                db.add(inner)
                await db.flush()
        except IntegrityError:
            pass
        await db.commit()
        rs = await db.scalars(select(User).where(User.name == "dup"))
        check = rs.first()
        return {"outer_id": outer_user.id, "dup_exists": check is not None}

    @app.post("/session/refresh/{uid}")
    async def refresh_test(uid: int, db: AsyncSession = Depends(get_db)):
        u = await db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        await db.execute(update(User).where(User.id == uid).values(name=f"changed-{uid}"))
        await db.commit()
        before = u.name
        await db.refresh(u)
        return {"before": before, "after": u.name}

    # === Tags arrays / JSON =============================================
    @app.post("/tagarr")
    async def add_tagarr(name: str, tags: list[str], db: AsyncSession = Depends(get_db)):
        t = TagArr(name=name, tags=tags)
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return {"id": t.id, "name": t.name, "tags": list(t.tags) if t.tags else []}

    @app.get("/tagarr/{tid}")
    async def get_tagarr(tid: int, db: AsyncSession = Depends(get_db)):
        t = await db.get(TagArr, tid)
        if not t:
            raise HTTPException(404, "not found")
        return {"id": t.id, "name": t.name, "tags": list(t.tags) if t.tags else []}

    @app.post("/items/{iid}/tags-json")
    async def set_tags_json(iid: int, tags_json: dict, db: AsyncSession = Depends(get_db)):
        it = await db.get(Item, iid)
        if not it:
            raise HTTPException(404, "not found")
        it.tags_json = tags_json
        await db.commit()
        await db.refresh(it)
        return {"id": it.id, "tags_json": it.tags_json}

    # === Errors =========================================================
    @app.post("/err/duplicate-email")
    async def err_dup(email: str, db: AsyncSession = Depends(get_db)):
        a = User(email=email, name="a", age=1)
        b = User(email=email, name="b", age=2)
        db.add(a)
        await db.commit()
        db.add(b)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return JSONResponse({"err": "integrity"}, status_code=409)
        return {"ok": True}

    @app.get("/err/no-result")
    async def err_no_result(db: AsyncSession = Depends(get_db)):
        rs = await db.scalars(select(User).where(User.id == -999))
        try:
            rs.one()
        except NoResultFound:
            return JSONResponse({"err": "no_result"}, status_code=404)
        return {"ok": True}

    @app.post("/err/multi-result")
    async def err_multi(db: AsyncSession = Depends(get_db)):
        db.add_all([User(email="m1@x", name="same", age=0), User(email="m2@x", name="same", age=0)])
        await db.commit()
        rs = await db.scalars(select(User).where(User.name == "same"))
        try:
            rs.one()
        except MultipleResultsFound:
            return JSONResponse({"err": "multi"}, status_code=400)
        return {"ok": True}

    # === Background tasks ===============================================
    BG_RESULTS: dict[str, int] = {}

    async def _bg_create_user(email: str, name: str, key: str):
        async with AsyncSessionLocal() as s:
            u = User(email=email, name=name, age=0)
            s.add(u)
            await s.commit()
            await s.refresh(u)
            BG_RESULTS[key] = u.id

    @app.post("/bg/create-user")
    async def bg_create(email: str, name: str, key: str, bt: BackgroundTasks):
        bt.add_task(_bg_create_user, email, name, key)
        return {"queued": True, "key": key}

    @app.get("/bg/result/{key}")
    async def bg_result(key: str):
        return {"key": key, "id": BG_RESULTS.get(key)}

    # === Composite PK ==================================================
    @app.post("/order-lines")
    async def create_order_line(
        order_id: int, line_no: int, sku: str, qty: int = 1,
        db: AsyncSession = Depends(get_db),
    ):
        ol = OrderLine(order_id=order_id, line_no=line_no, sku=sku, qty=qty)
        db.add(ol)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "dup")
        return {"order_id": order_id, "line_no": line_no, "sku": sku, "qty": qty}

    @app.get("/order-lines/{oid}/{ln}")
    async def get_order_line(oid: int, ln: int, db: AsyncSession = Depends(get_db)):
        ol = await db.get(OrderLine, (oid, ln))
        if not ol:
            raise HTTPException(404, "not found")
        return {"order_id": ol.order_id, "line_no": ol.line_no, "sku": ol.sku, "qty": ol.qty}

    # === Status enum ====================================================
    @app.post("/items/{iid}/status")
    async def set_status(iid: int, status: StatusEnum, db: AsyncSession = Depends(get_db)):
        it = await db.get(Item, iid)
        if not it:
            raise HTTPException(404, "not found")
        it.status = status
        await db.commit()
        await db.refresh(it)
        return {"id": it.id, "status": it.status.value}

    # === Pagination =====================================================
    @app.get("/q/items-page")
    async def items_page(page: int = 1, size: int = 10, db: AsyncSession = Depends(get_db)):
        stmt = select(Item).order_by(Item.id).limit(size).offset((page - 1) * size)
        rs = await db.scalars(stmt)
        rows = list(rs.all())
        total = await db.scalar(select(func.count(Item.id))) or 0
        return {
            "total": int(total),
            "page": page,
            "size": size,
            "items": [{"id": r.id, "title": r.title} for r in rows],
        }

    # === Counts + reset =================================================
    @app.get("/__count/users")
    async def count_users(db: AsyncSession = Depends(get_db)):
        return {"n": int(await db.scalar(select(func.count(User.id))) or 0)}

    @app.get("/__count/items")
    async def count_items(db: AsyncSession = Depends(get_db)):
        return {"n": int(await db.scalar(select(func.count(Item.id))) or 0)}

    @app.get("/__count/categories")
    async def count_cats(db: AsyncSession = Depends(get_db)):
        return {"n": int(await db.scalar(select(func.count(Category.id))) or 0)}

    @app.get("/__reset")
    async def reset_db(db: AsyncSession = Depends(get_db)):
        await db.execute(delete(Item))
        await db.execute(delete(Category))
        await db.execute(delete(User))
        await db.execute(delete(OrderLine))
        await db.execute(delete(TagArr))
        await db.commit()
        return {"ok": True}

    return app
