"""Complex E-commerce API — worst-case benchmark.

This app exercises the hardest paths:
- Deep dependency chains (4 levels)
- Large Pydantic models with nested objects
- response_model with include/exclude filtering
- Multiple body parameters
- Error handling with HTTPException
- Larger JSON payloads (~500 bytes)
- CORS + auth on every protected route
"""
import fastapi_rs
from fastapi_rs import FastAPI, Depends, Header, Query, HTTPException, Body
from fastapi_rs.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
import time
import hashlib

app = FastAPI(title="Complex E-commerce", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Large nested models ──────────────────────────────────────────────

class Address(BaseModel):
    street: str
    city: str
    state: str
    zip_code: str
    country: str = "US"

class UserProfile(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    address: Address
    is_active: bool = True
    created_at: str = "2024-01-01T00:00:00Z"

class UserOut(BaseModel):
    id: int
    username: str
    email: str
    full_name: str

class Category(BaseModel):
    id: int
    name: str
    description: str = ""

class ProductImage(BaseModel):
    url: str
    alt_text: str = ""
    width: int = 0
    height: int = 0

class Product(BaseModel):
    id: int
    name: str
    description: str
    price: float
    category: Category
    images: list[ProductImage] = []
    tags: list[str] = []
    stock: int = 0
    sku: str = ""
    weight: float = 0.0
    is_active: bool = True

class ProductCreate(BaseModel):
    name: str
    description: str = ""
    price: float = Field(gt=0)
    category_id: int
    tags: list[str] = []
    stock: int = Field(ge=0, default=0)
    sku: str = ""
    weight: float = Field(ge=0, default=0.0)

class ProductOut(BaseModel):
    id: int
    name: str
    price: float
    category: Category
    tags: list[str]
    stock: int

class OrderItem(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)
    unit_price: float

class OrderCreate(BaseModel):
    items: list[OrderItem]
    shipping_address: Address
    notes: str = ""

class OrderOut(BaseModel):
    id: int
    items: list[OrderItem]
    total: float
    status: str
    created_at: str

# ── In-memory database ───────────────────────────────────────────────

categories_db = {
    1: {"id": 1, "name": "Electronics", "description": "Electronic devices and gadgets"},
    2: {"id": 2, "name": "Clothing", "description": "Apparel and fashion"},
}

products_db = {
    1: {
        "id": 1, "name": "Wireless Headphones", "description": "Premium noise-cancelling wireless headphones with 30-hour battery life",
        "price": 149.99, "category": categories_db[1],
        "images": [{"url": "https://example.com/img/headphones.jpg", "alt_text": "Headphones front view", "width": 800, "height": 600}],
        "tags": ["audio", "wireless", "premium"], "stock": 150, "sku": "WH-001", "weight": 0.35, "is_active": True,
    },
    2: {
        "id": 2, "name": "Running Shoes", "description": "Lightweight running shoes with responsive cushioning",
        "price": 89.99, "category": categories_db[2],
        "images": [{"url": "https://example.com/img/shoes.jpg", "alt_text": "Shoes side view", "width": 800, "height": 600}],
        "tags": ["running", "sports", "comfortable"], "stock": 200, "sku": "RS-002", "weight": 0.6, "is_active": True,
    },
}

users_db = {
    1: {
        "id": 1, "username": "alice", "email": "alice@example.com", "full_name": "Alice Johnson",
        "address": {"street": "123 Main St", "city": "Springfield", "state": "IL", "zip_code": "62701", "country": "US"},
        "is_active": True, "created_at": "2024-01-15T10:30:00Z",
    },
}

orders_db = {}
next_order_id = 1
next_product_id = 3

# ── 4-level dependency chain ─────────────────────────────────────────

def get_db():
    """Level 1: Database connection."""
    return {"products": products_db, "users": users_db, "orders": orders_db, "categories": categories_db}

def get_settings():
    """Level 1: App settings."""
    return {"max_items_per_page": 50, "tax_rate": 0.08}

def verify_token(authorization: str = Header("none")):
    """Level 2: Token verification (depends on nothing, but called by get_current_user)."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    token = authorization[7:]
    if token != "secret-token-123":
        raise HTTPException(status_code=401, detail="Invalid token")
    return token

def get_current_user(token=Depends(verify_token), db=Depends(get_db)):
    """Level 3: Get current user (depends on verify_token + get_db)."""
    return users_db[1]

def get_admin_user(user=Depends(get_current_user), settings=Depends(get_settings)):
    """Level 4: Verify admin permissions (depends on get_current_user + get_settings)."""
    if user["username"] != "alice":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ── Endpoints ────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/products", response_model=list[ProductOut])
def list_products(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    category_id: int = Query(None),
    db=Depends(get_db),
    settings=Depends(get_settings),
):
    products = list(db["products"].values())
    if category_id is not None:
        products = [p for p in products if p["category"]["id"] == category_id]
    return products[offset:offset + min(limit, settings["max_items_per_page"])]

@app.get("/products/{product_id}", response_model=Product)
def get_product(product_id: int, db=Depends(get_db)):
    if product_id not in db["products"]:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    return db["products"][product_id]

@app.post("/products", response_model=ProductOut, status_code=201)
def create_product(product: ProductCreate, admin=Depends(get_admin_user), db=Depends(get_db)):
    global next_product_id
    cat = db["categories"].get(product.category_id)
    if not cat:
        raise HTTPException(status_code=400, detail="Invalid category_id")
    new = {
        "id": next_product_id, "name": product.name, "description": product.description,
        "price": product.price, "category": cat, "images": [],
        "tags": product.tags, "stock": product.stock, "sku": product.sku,
        "weight": product.weight, "is_active": True,
    }
    db["products"][next_product_id] = new
    next_product_id += 1
    return new

@app.get("/users/me", response_model=UserOut)
def get_me(user=Depends(get_current_user)):
    return user

@app.get("/users/me/profile", response_model=UserProfile)
def get_profile(user=Depends(get_current_user), db=Depends(get_db)):
    return user

@app.post("/orders", response_model=OrderOut, status_code=201)
def create_order(order: OrderCreate, user=Depends(get_current_user), db=Depends(get_db)):
    global next_order_id
    total = sum(item.unit_price * item.quantity for item in order.items)
    new_order = {
        "id": next_order_id,
        "items": [{"product_id": i.product_id, "quantity": i.quantity, "unit_price": i.unit_price} for i in order.items],
        "total": round(total * 1.08, 2),
        "status": "pending",
        "created_at": "2024-06-15T12:00:00Z",
    }
    db["orders"][next_order_id] = new_order
    next_order_id += 1
    return new_order

app.run(host="127.0.0.1", port=19020)
