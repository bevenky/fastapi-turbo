"""Shared app factory for SYNC SQLAlchemy drivers (psycopg3 sync, psycopg2).

Each endpoint exercises a specific SQLAlchemy pattern. About 70 sync routes.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import (
    and_,
    between,
    create_engine,
    delete,
    except_ as sql_except,
    func,
    insert,
    intersect,
    not_,
    or_,
    select,
    text,
    union,
    update,
)
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    MultipleResultsFound,
    NoResultFound,
    OperationalError,
    PendingRollbackError,
)
from sqlalchemy.orm import (
    Session,
    contains_eager,
    joinedload,
    scoped_session,
    selectinload,
    sessionmaker,
    subqueryload,
)
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

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
    # ---- engine + sessionmaker ------------------------------------------------
    if IS_SQLITE:
        engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(
            db_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=300,
        )
    SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)
    ScopedSession = scoped_session(SessionLocal)

    # Create all tables
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    app = FastAPI()

    def get_db():
        db: Session = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # ---- routes --------------------------------------------------------------
    @app.get("/health")
    def health():
        return {"ok": True}

    # === Users CRUD =========================================================
    @app.post("/users", response_model=UserOut)
    def create_user(payload: UserIn, db: Session = Depends(get_db)):
        u = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(u)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "duplicate email")
        db.refresh(u)
        return u

    @app.get("/users/{uid}", response_model=UserOut)
    def get_user(uid: int, db: Session = Depends(get_db)):
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        return u

    @app.get("/users", response_model=list[UserOut])
    def list_users(
        db: Session = Depends(get_db),
        limit: int = 50,
        offset: int = 0,
        order: str = "id",
    ):
        col = getattr(User, order, User.id)
        stmt = select(User).order_by(col).limit(limit).offset(offset)
        return list(db.scalars(stmt).all())

    @app.put("/users/{uid}", response_model=UserOut)
    def update_user(uid: int, payload: UserIn, db: Session = Depends(get_db)):
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        u.email = payload.email
        u.name = payload.name
        u.age = payload.age
        db.commit()
        db.refresh(u)
        return u

    @app.delete("/users/{uid}")
    def delete_user(uid: int, db: Session = Depends(get_db)):
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        db.delete(u)
        db.commit()
        return {"deleted": uid}

    @app.get("/users/by-email/{email}", response_model=UserOut)
    def user_by_email(email: str, db: Session = Depends(get_db)):
        stmt = select(User).where(User.email == email)
        try:
            u = db.scalars(stmt).one()
        except NoResultFound:
            raise HTTPException(404, "not found")
        return u

    @app.get("/users/{uid}/or-none")
    def user_or_none(uid: int, db: Session = Depends(get_db)):
        stmt = select(User).where(User.id == uid)
        u = db.scalars(stmt).one_or_none()
        return {"found": u is not None, "id": u.id if u else None}

    @app.get("/q/users-first")
    def first_user(db: Session = Depends(get_db)):
        u = db.scalars(select(User).order_by(User.id)).first()
        return {"id": u.id if u else None}

    # === Categories =========================================================
    @app.post("/categories", response_model=CategoryOut)
    def create_category(payload: CategoryIn, db: Session = Depends(get_db)):
        c = Category(name=payload.name)
        db.add(c)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "duplicate")
        db.refresh(c)
        return c

    @app.get("/categories", response_model=list[CategoryOut])
    def list_categories(db: Session = Depends(get_db)):
        return list(db.scalars(select(Category).order_by(Category.id)).all())

    # === Items CRUD + relationships =========================================
    @app.post("/items", response_model=ItemOut)
    def create_item(payload: ItemIn, db: Session = Depends(get_db)):
        owner = db.get(User, payload.owner_id)
        if not owner:
            raise HTTPException(400, "owner missing")
        it = Item(**payload.model_dump())
        db.add(it)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise HTTPException(409, "integrity")
        db.refresh(it)
        return it

    @app.get("/items/{iid}", response_model=ItemOut)
    def get_item(iid: int, db: Session = Depends(get_db)):
        it = db.get(Item, iid)
        if not it:
            raise HTTPException(404, "not found")
        return it

    @app.get("/items/{iid}/with-owner", response_model=ItemWithOwnerOut)
    def item_with_owner(iid: int, db: Session = Depends(get_db)):
        stmt = select(Item).options(joinedload(Item.owner)).where(Item.id == iid)
        it = db.scalars(stmt).unique().one_or_none()
        if not it:
            raise HTTPException(404, "not found")
        return it

    @app.get("/q/items-by-owner/{uid}", response_model=list[ItemOut])
    def items_by_owner(uid: int, db: Session = Depends(get_db)):
        stmt = (
            select(Item)
            .where(Item.owner_id == uid)
            .order_by(Item.id)
        )
        return list(db.scalars(stmt).all())

    @app.get("/q/items-by-owner-selectin/{uid}")
    def items_by_owner_selectin(uid: int, db: Session = Depends(get_db)):
        stmt = (
            select(User)
            .options(selectinload(User.items))
            .where(User.id == uid)
        )
        u = db.scalars(stmt).one_or_none()
        if not u:
            raise HTTPException(404, "not found")
        return {"owner": u.name, "count": len(u.items), "ids": sorted([i.id for i in u.items])}

    @app.get("/q/items-by-owner-subqueryload/{uid}")
    def items_by_owner_subq(uid: int, db: Session = Depends(get_db)):
        stmt = (
            select(User)
            .options(subqueryload(User.items))
            .where(User.id == uid)
        )
        u = db.scalars(stmt).one_or_none()
        if not u:
            raise HTTPException(404, "not found")
        return {"count": len(u.items)}

    @app.get("/q/items-by-owner-joinedload/{uid}")
    def items_by_owner_joined(uid: int, db: Session = Depends(get_db)):
        stmt = (
            select(User)
            .options(joinedload(User.items))
            .where(User.id == uid)
        )
        u = db.scalars(stmt).unique().one_or_none()
        if not u:
            raise HTTPException(404, "not found")
        return {"count": len(u.items)}

    # === Filter expressions =================================================
    @app.get("/q/items-filter")
    def filter_items(
        db: Session = Depends(get_db),
        min_price: float = 0.0,
        max_price: float = 1e12,
        status: Optional[StatusEnum] = None,
        title_like: Optional[str] = None,
    ):
        stmt = select(Item).where(between(Item.price, min_price, max_price))
        if status is not None:
            stmt = stmt.where(Item.status == status)
        if title_like:
            stmt = stmt.where(Item.title.like(f"%{title_like}%"))
        stmt = stmt.order_by(Item.id)
        out = [{"id": i.id, "title": i.title, "price": i.price} for i in db.scalars(stmt).all()]
        return {"count": len(out), "items": out}

    @app.get("/q/items-filter-ilike")
    def filter_ilike(pat: str, db: Session = Depends(get_db)):
        stmt = select(Item).where(Item.title.ilike(f"%{pat}%")).order_by(Item.id)
        return [{"id": i.id, "title": i.title} for i in db.scalars(stmt).all()]

    @app.get("/q/items-filter-in")
    def filter_in(db: Session = Depends(get_db), ids: str = ""):
        id_list = [int(x) for x in ids.split(",") if x]
        if not id_list:
            return []
        stmt = select(Item).where(Item.id.in_(id_list)).order_by(Item.id)
        return [{"id": i.id} for i in db.scalars(stmt).all()]

    @app.get("/q/items-filter-and")
    def filter_and(db: Session = Depends(get_db), min_q: int = 0, max_p: float = 1e12):
        stmt = select(Item).where(and_(Item.quantity >= min_q, Item.price <= max_p)).order_by(Item.id)
        return [{"id": i.id} for i in db.scalars(stmt).all()]

    @app.get("/q/items-filter-or")
    def filter_or(db: Session = Depends(get_db), q: int = 0, p: float = 0):
        stmt = select(Item).where(or_(Item.quantity == q, Item.price == p)).order_by(Item.id)
        return [{"id": i.id} for i in db.scalars(stmt).all()]

    @app.get("/q/items-filter-not")
    def filter_not(db: Session = Depends(get_db), status: StatusEnum = StatusEnum.draft):
        stmt = select(Item).where(not_(Item.status == status)).order_by(Item.id)
        return [{"id": i.id} for i in db.scalars(stmt).all()]

    # === Aggregates =========================================================
    @app.get("/q/items-stats")
    def item_stats(db: Session = Depends(get_db)):
        row = db.execute(
            select(
                func.count(Item.id),
                func.coalesce(func.sum(Item.price), 0),
                func.coalesce(func.avg(Item.price), 0),
                func.coalesce(func.max(Item.price), 0),
                func.coalesce(func.min(Item.price), 0),
            )
        ).one()
        return {
            "count": int(row[0]),
            "sum": float(row[1]),
            "avg": float(row[2]),
            "max": float(row[3]),
            "min": float(row[4]),
        }

    @app.get("/q/items-stats-by-status")
    def stats_by_status(db: Session = Depends(get_db)):
        stmt = (
            select(Item.status, func.count(Item.id))
            .group_by(Item.status)
            .order_by(Item.status)
        )
        rows = db.execute(stmt).all()
        return [{"status": r[0].value if hasattr(r[0], "value") else r[0], "count": int(r[1])} for r in rows]

    @app.get("/q/items-having")
    def having_test(db: Session = Depends(get_db), min_count: int = 1):
        stmt = (
            select(Item.owner_id, func.count(Item.id))
            .group_by(Item.owner_id)
            .having(func.count(Item.id) >= min_count)
            .order_by(Item.owner_id)
        )
        return [{"owner_id": r[0], "count": int(r[1])} for r in db.execute(stmt).all()]

    # === Joins ==============================================================
    @app.get("/q/items-join-owner")
    def items_join_owner(db: Session = Depends(get_db)):
        stmt = (
            select(Item.id, User.name)
            .join(User, Item.owner_id == User.id)
            .order_by(Item.id)
        )
        return [{"item_id": r[0], "owner_name": r[1]} for r in db.execute(stmt).all()]

    @app.get("/q/items-left-join-category")
    def items_left_join_cat(db: Session = Depends(get_db)):
        stmt = (
            select(Item.id, Category.name)
            .join(Category, Item.category_id == Category.id, isouter=True)
            .order_by(Item.id)
        )
        return [{"item_id": r[0], "category": r[1]} for r in db.execute(stmt).all()]

    @app.get("/q/items-contains-eager/{uid}")
    def contains_eager_test(uid: int, db: Session = Depends(get_db)):
        stmt = (
            select(User)
            .join(User.items)
            .options(contains_eager(User.items))
            .where(User.id == uid)
        )
        u = db.scalars(stmt).unique().one_or_none()
        if not u:
            raise HTTPException(404, "not found")
        return {"count": len(u.items)}

    # === Subqueries / CTEs / Window =========================================
    @app.get("/q/items-subquery-max")
    def sub_max_per_owner(db: Session = Depends(get_db)):
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
        rows = db.execute(stmt).all()
        return [{"id": r[0], "price": float(r[1]), "max": float(r[2])} for r in rows]

    @app.get("/q/items-cte-max")
    def cte_max(db: Session = Depends(get_db)):
        cte = (
            select(Item.owner_id, func.max(Item.price).label("mx"))
            .group_by(Item.owner_id)
            .cte("mx")
        )
        stmt = select(cte.c.owner_id, cte.c.mx).order_by(cte.c.owner_id)
        return [{"owner_id": r[0], "max": float(r[1])} for r in db.execute(stmt).all()]

    @app.get("/q/items-window")
    def window_fn(db: Session = Depends(get_db)):
        stmt = select(
            Item.id,
            Item.owner_id,
            Item.price,
            func.row_number().over(partition_by=Item.owner_id, order_by=Item.price.desc()).label("rn"),
        ).order_by(Item.id)
        rows = db.execute(stmt).all()
        return [{"id": r[0], "owner": r[1], "price": float(r[2]), "rn": int(r[3])} for r in rows]

    # === Raw SQL ============================================================
    @app.get("/raw/count-users")
    def raw_count_users(db: Session = Depends(get_db)):
        row = db.execute(text(f"SELECT COUNT(*) AS n FROM {User.__tablename__}")).one()
        return {"n": int(row[0])}

    @app.get("/raw/user-name/{uid}")
    def raw_user_name(uid: int, db: Session = Depends(get_db)):
        row = db.execute(
            text(f"SELECT name FROM {User.__tablename__} WHERE id = :id"),
            {"id": uid},
        ).one_or_none()
        return {"name": row[0] if row else None}

    # === Set operations =====================================================
    @app.get("/q/items-union")
    def union_test(db: Session = Depends(get_db)):
        a = select(Item.id).where(Item.status == StatusEnum.draft)
        b = select(Item.id).where(Item.status == StatusEnum.active)
        stmt = union(a, b).order_by("id")
        return [{"id": r[0]} for r in db.execute(stmt).all()]

    @app.get("/q/items-intersect")
    def intersect_test(db: Session = Depends(get_db)):
        a = select(Item.id).where(Item.price >= 0)
        b = select(Item.id).where(Item.quantity >= 0)
        stmt = intersect(a, b).order_by("id")
        return [{"id": r[0]} for r in db.execute(stmt).all()]

    # === Bulk operations ====================================================
    @app.post("/bulk/items")
    def bulk_items(payload: list[ItemIn], db: Session = Depends(get_db)):
        objs = [Item(**p.model_dump()) for p in payload]
        db.add_all(objs)
        db.commit()
        return {"inserted": len(objs)}

    @app.post("/bulk/insert-core")
    def bulk_insert_core(payload: list[ItemIn], db: Session = Depends(get_db)):
        stmt = insert(Item).values([p.model_dump() for p in payload])
        result = db.execute(stmt)
        db.commit()
        return {"rowcount": result.rowcount}

    @app.post("/bulk/update-core")
    def bulk_update(owner_id: int, new_price: float, db: Session = Depends(get_db)):
        stmt = update(Item).where(Item.owner_id == owner_id).values(price=new_price)
        result = db.execute(stmt)
        db.commit()
        return {"rowcount": result.rowcount}

    @app.post("/bulk/delete-core")
    def bulk_delete(owner_id: int, db: Session = Depends(get_db)):
        stmt = delete(Item).where(Item.owner_id == owner_id)
        result = db.execute(stmt)
        db.commit()
        return {"rowcount": result.rowcount}

    @app.post("/returning/insert-item")
    def insert_returning(payload: ItemIn, db: Session = Depends(get_db)):
        stmt = (
            insert(Item)
            .values(**payload.model_dump())
            .returning(Item.id, Item.title)
        )
        row = db.execute(stmt).one()
        db.commit()
        return {"id": row[0], "title": row[1]}

    # === Session lifecycle ==================================================
    @app.post("/session/flush-no-commit")
    def flush_no_commit(payload: UserIn, db: Session = Depends(get_db)):
        u = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(u)
        db.flush()
        uid = u.id
        db.rollback()
        # Also check it's gone
        check = db.get(User, uid)
        return {"assigned_id_truthy": bool(uid), "post_rollback_found": check is not None}

    @app.post("/session/savepoint")
    def savepoint_test(payload: UserIn, db: Session = Depends(get_db)):
        outer_user = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(outer_user)
        db.flush()
        with db.begin_nested():
            inner = User(email=f"inner-{payload.email}", name="inner", age=1)
            db.add(inner)
            db.flush()
            inner_id = inner.id
            # savepoint rollback
        # Actually let's commit outer and abort inner explicitly
        db.commit()
        # outer should exist, inner was added inside the nested block; since we didn't raise,
        # SQLAlchemy released the savepoint, so inner should be there too.
        return {"outer_id": outer_user.id, "inner_id_was": inner_id}

    @app.post("/session/savepoint-rollback")
    def savepoint_rollback(payload: UserIn, db: Session = Depends(get_db)):
        outer_user = User(email=payload.email, name=payload.name, age=payload.age)
        db.add(outer_user)
        db.flush()
        try:
            with db.begin_nested():
                inner = User(email=payload.email, name="dup", age=2)  # dup email
                db.add(inner)
                db.flush()
        except IntegrityError:
            pass
        db.commit()
        # outer persisted, inner not
        check = db.scalars(
            select(User).where(User.name == "dup")
        ).first()
        return {"outer_id": outer_user.id, "dup_exists": check is not None}

    @app.post("/session/refresh/{uid}")
    def refresh_test(uid: int, db: Session = Depends(get_db)):
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        # Modify behind its back
        db.execute(update(User).where(User.id == uid).values(name=f"changed-{uid}"))
        db.commit()
        before = u.name
        db.refresh(u)
        return {"before": before, "after": u.name}

    @app.post("/session/expire/{uid}")
    def expire_test(uid: int, db: Session = Depends(get_db)):
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        db.execute(update(User).where(User.id == uid).values(name=f"ex-{uid}"))
        db.commit()
        db.expire(u)
        # lazy load triggers re-fetch
        return {"name": u.name}

    @app.post("/session/expunge/{uid}")
    def expunge_test(uid: int, db: Session = Depends(get_db)):
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "not found")
        db.expunge(u)
        return {"detached_id": u.id, "name": u.name}

    @app.post("/session/merge/{uid}")
    def merge_test(uid: int, new_name: str, db: Session = Depends(get_db)):
        existing = db.get(User, uid)
        if not existing:
            raise HTTPException(404, "not found")
        db.expunge(existing)
        existing.name = new_name
        merged = db.merge(existing)
        db.commit()
        return {"merged_name": merged.name}

    @app.get("/session/scoped")
    def scoped_count():
        # Use ScopedSession — thread local
        db = ScopedSession()
        try:
            n = db.scalar(select(func.count(User.id)))
            return {"n": int(n or 0)}
        finally:
            ScopedSession.remove()

    # === Composite PK =======================================================
    @app.post("/order-lines")
    def create_order_line(
        order_id: int, line_no: int, sku: str, qty: int = 1,
        db: Session = Depends(get_db),
    ):
        ol = OrderLine(order_id=order_id, line_no=line_no, sku=sku, qty=qty)
        db.add(ol)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "dup")
        return {"order_id": order_id, "line_no": line_no, "sku": sku, "qty": qty}

    @app.get("/order-lines/{oid}/{ln}")
    def get_order_line(oid: int, ln: int, db: Session = Depends(get_db)):
        ol = db.get(OrderLine, (oid, ln))
        if not ol:
            raise HTTPException(404, "not found")
        return {"order_id": ol.order_id, "line_no": ol.line_no, "sku": ol.sku, "qty": ol.qty}

    # === Tags arrays / JSON =================================================
    @app.post("/tagarr")
    def add_tagarr(name: str, tags: list[str], db: Session = Depends(get_db)):
        t = TagArr(name=name, tags=tags)
        db.add(t)
        db.commit()
        db.refresh(t)
        return {"id": t.id, "name": t.name, "tags": list(t.tags) if t.tags else []}

    @app.get("/tagarr/{tid}")
    def get_tagarr(tid: int, db: Session = Depends(get_db)):
        t = db.get(TagArr, tid)
        if not t:
            raise HTTPException(404, "not found")
        return {"id": t.id, "name": t.name, "tags": list(t.tags) if t.tags else []}

    @app.post("/items/{iid}/tags-json")
    def set_tags_json(iid: int, tags_json: dict, db: Session = Depends(get_db)):
        it = db.get(Item, iid)
        if not it:
            raise HTTPException(404, "not found")
        it.tags_json = tags_json
        db.commit()
        db.refresh(it)
        return {"id": it.id, "tags_json": it.tags_json}

    # === Error propagation ==================================================
    @app.post("/err/duplicate-email")
    def err_dup(email: str, db: Session = Depends(get_db)):
        a = User(email=email, name="a", age=1)
        b = User(email=email, name="b", age=2)
        db.add(a)
        db.commit()
        db.add(b)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return JSONResponse({"err": "integrity"}, status_code=409)
        return {"ok": True}

    @app.get("/err/no-result")
    def err_no_result(db: Session = Depends(get_db)):
        try:
            db.scalars(select(User).where(User.id == -999)).one()
        except NoResultFound:
            return JSONResponse({"err": "no_result"}, status_code=404)
        return {"ok": True}

    @app.post("/err/multi-result")
    def err_multi_result(db: Session = Depends(get_db)):
        # insert two with same name
        db.add_all([User(email="m1@x", name="same", age=0), User(email="m2@x", name="same", age=0)])
        db.commit()
        try:
            db.scalars(select(User).where(User.name == "same")).one()
        except MultipleResultsFound:
            return JSONResponse({"err": "multi"}, status_code=400)
        return {"ok": True}

    @app.post("/err/pending-rollback")
    def err_pending_rollback(email: str, db: Session = Depends(get_db)):
        u = User(email=email, name="a", age=1)
        db.add(u)
        db.commit()
        dup = User(email=email, name="b", age=2)
        db.add(dup)
        try:
            db.commit()
        except IntegrityError:
            pass
        # don't rollback; now try something — should raise PendingRollbackError
        try:
            db.scalar(select(func.count(User.id)))
        except PendingRollbackError:
            db.rollback()
            return {"err": "pending"}
        return {"ok": True}

    # === Pydantic from_attributes ===========================================
    @app.get("/q/items-latest", response_model=ItemOut)
    def latest_item(db: Session = Depends(get_db)):
        it = db.scalars(select(Item).order_by(Item.id.desc()).limit(1)).first()
        if not it:
            raise HTTPException(404, "empty")
        return it

    # === Background tasks with separate session =============================
    BG_RESULTS: dict[str, int] = {}

    def _bg_create_user(email: str, name: str, key: str):
        s = SessionLocal()
        try:
            u = User(email=email, name=name, age=0)
            s.add(u)
            s.commit()
            s.refresh(u)
            BG_RESULTS[key] = u.id
        finally:
            s.close()

    @app.post("/bg/create-user")
    def bg_create(email: str, name: str, key: str, bt: BackgroundTasks):
        bt.add_task(_bg_create_user, email, name, key)
        return {"queued": True, "key": key}

    @app.get("/bg/result/{key}")
    def bg_result(key: str):
        return {"key": key, "id": BG_RESULTS.get(key)}

    # === Engine-level transaction ==========================================
    @app.post("/engine/begin")
    def engine_begin_test(email: str, name: str):
        with engine.begin() as conn:
            r = conn.execute(
                text(f"INSERT INTO {User.__tablename__} (email, name, age, is_active, created_at) "
                     f"VALUES (:e, :n, 0, TRUE, CURRENT_TIMESTAMP) RETURNING id"),
                {"e": email, "n": name},
            ).one()
            return {"id": int(r[0])}

    # === Status + enum roundtrip ============================================
    @app.post("/items/{iid}/status")
    def set_status(iid: int, status: StatusEnum, db: Session = Depends(get_db)):
        it = db.get(Item, iid)
        if not it:
            raise HTTPException(404, "not found")
        it.status = status
        db.commit()
        db.refresh(it)
        return {"id": it.id, "status": it.status.value}

    # === Pagination helper ==================================================
    @app.get("/q/items-page")
    def items_page(page: int = 1, size: int = 10, db: Session = Depends(get_db)):
        stmt = (
            select(Item)
            .order_by(Item.id)
            .limit(size)
            .offset((page - 1) * size)
        )
        rows = db.scalars(stmt).all()
        total = db.scalar(select(func.count(Item.id))) or 0
        return {
            "total": int(total),
            "page": page,
            "size": size,
            "items": [{"id": r.id, "title": r.title} for r in rows],
        }

    # === Count endpoint for verification =====================================
    @app.get("/__count/users")
    def count_users(db: Session = Depends(get_db)):
        return {"n": int(db.scalar(select(func.count(User.id))) or 0)}

    @app.get("/__count/items")
    def count_items(db: Session = Depends(get_db)):
        return {"n": int(db.scalar(select(func.count(Item.id))) or 0)}

    @app.get("/__count/categories")
    def count_categories(db: Session = Depends(get_db)):
        return {"n": int(db.scalar(select(func.count(Category.id))) or 0)}

    @app.get("/__reset")
    def reset_db(db: Session = Depends(get_db)):
        db.execute(delete(Item))
        db.execute(delete(Category))
        db.execute(delete(User))
        db.execute(delete(OrderLine))
        db.execute(delete(TagArr))
        db.commit()
        return {"ok": True}

    return app
