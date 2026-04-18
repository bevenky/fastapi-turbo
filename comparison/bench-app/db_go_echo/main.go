// Database benchmark API -- Go Echo implementation.
//
// Tests real PostgreSQL (pgx) and Redis (go-redis) performance.
// Same endpoints as Go Gin version, adapted for Echo framework.
//
// Build: cd db_go_echo && go mod tidy && go build -o db-echo .
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"
	"github.com/redis/go-redis/v9"
)

var (
	dbPool      *pgxpool.Pool
	redisClient *redis.Client
)

// ── Models ─────────────────────────────────────────────────────────────

type ProductOut struct {
	ID           int     `json:"id"`
	Name         string  `json:"name"`
	Price        float64 `json:"price"`
	Stock        int     `json:"stock"`
	CategoryName string  `json:"category_name"`
}

type ProductCreate struct {
	Name        string  `json:"name"`
	Description string  `json:"description"`
	Price       float64 `json:"price"`
	CategoryID  int     `json:"category_id"`
	Stock       int     `json:"stock"`
}

type CategoryStats struct {
	ID           int     `json:"id"`
	Name         string  `json:"name"`
	ProductCount int     `json:"product_count"`
	AvgPrice     float64 `json:"avg_price"`
	TotalStock   int     `json:"total_stock"`
}

type OrderOut struct {
	ID        int       `json:"id"`
	UserID    int       `json:"user_id"`
	Total     float64   `json:"total"`
	Status    string    `json:"status"`
	CreatedAt time.Time `json:"created_at"`
}

type OrderItemOut struct {
	ID          int     `json:"id"`
	OrderID     int     `json:"order_id"`
	ProductID   int     `json:"product_id"`
	Quantity    int     `json:"quantity"`
	UnitPrice   float64 `json:"unit_price"`
	ProductName string  `json:"product_name"`
}

// ── Main ───────────────────────────────────────────────────────────────

func main() {
	ctx := context.Background()

	// PostgreSQL connection pool (same size: min=5, max=20)
	poolConfig, err := pgxpool.ParseConfig("postgresql://venky@localhost/fastapi_rs_bench")
	if err != nil {
		log.Fatalf("Failed to parse PG config: %v", err)
	}
	poolConfig.MinConns = 5
	poolConfig.MaxConns = 20

	dbPool, err = pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		log.Fatalf("Failed to create PG pool: %v", err)
	}
	defer dbPool.Close()

	// Verify PG connection
	if err := dbPool.Ping(ctx); err != nil {
		log.Fatalf("Failed to ping PG: %v", err)
	}

	// Redis client
	redisClient = redis.NewClient(&redis.Options{
		Addr: "localhost:6379",
	})
	defer redisClient.Close()

	// Verify Redis connection
	if _, err := redisClient.Ping(ctx).Result(); err != nil {
		log.Fatalf("Failed to ping Redis: %v", err)
	}

	// Echo setup
	e := echo.New()
	e.HideBanner = true
	e.HidePort = true
	e.Use(middleware.CORSWithConfig(middleware.CORSConfig{
		AllowOrigins: []string{"*"},
		AllowMethods: []string{http.MethodGet, http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodOptions},
		AllowHeaders: []string{"*"},
	}))

	// Routes
	e.GET("/health", healthHandler)
	e.GET("/products/:id", getProductHandler)
	e.GET("/products", listProductsHandler)
	e.POST("/products", createProductHandler)
	e.PUT("/products/:id", updateProductHandler)
	e.PATCH("/products/:id", patchProductHandler)
	e.DELETE("/products/:id", deleteProductHandler)
	e.GET("/categories/stats", categoryStatsHandler)
	e.GET("/cached/products/:id", getCachedProductHandler)
	e.GET("/orders/:id", getOrderHandler)

	port := os.Getenv("PORT")
	if port == "" {
		port = "19033"
	}
	log.Printf("Go Echo DB server listening on :%s", port)
	e.Logger.Fatal(e.Start("127.0.0.1:" + port))
}

// ── Handlers ───────────────────────────────────────────────────────────

func healthHandler(c echo.Context) error {
	return c.JSON(http.StatusOK, map[string]string{"status": "ok"})
}

// Simple query: SELECT single row with JOIN
func getProductHandler(c echo.Context) error {
	id, err := strconv.Atoi(c.Param("id"))
	if err != nil {
		return c.JSON(http.StatusBadRequest, map[string]string{"detail": "Invalid product ID"})
	}

	var p ProductOut
	err = dbPool.QueryRow(c.Request().Context(),
		"SELECT p.id, p.name, p.price, p.stock, c.name as category_name "+
			"FROM products p JOIN categories c ON p.category_id = c.id "+
			"WHERE p.id = $1", id,
	).Scan(&p.ID, &p.Name, &p.Price, &p.Stock, &p.CategoryName)

	if err != nil {
		return c.JSON(http.StatusNotFound, map[string]string{"detail": "Product not found"})
	}
	return c.JSON(http.StatusOK, p)
}

// List with pagination
func listProductsHandler(c echo.Context) error {
	limitStr := c.QueryParam("limit")
	offsetStr := c.QueryParam("offset")
	if limitStr == "" {
		limitStr = "10"
	}
	if offsetStr == "" {
		offsetStr = "0"
	}
	limit, _ := strconv.Atoi(limitStr)
	offset, _ := strconv.Atoi(offsetStr)

	rows, err := dbPool.Query(c.Request().Context(),
		"SELECT p.id, p.name, p.price, p.stock, c.name as category_name "+
			"FROM products p JOIN categories c ON p.category_id = c.id "+
			"ORDER BY p.id LIMIT $1 OFFSET $2", limit, offset,
	)
	if err != nil {
		return c.JSON(http.StatusInternalServerError, map[string]string{"detail": err.Error()})
	}
	defer rows.Close()

	products := make([]ProductOut, 0)
	for rows.Next() {
		var p ProductOut
		if err := rows.Scan(&p.ID, &p.Name, &p.Price, &p.Stock, &p.CategoryName); err != nil {
			continue
		}
		products = append(products, p)
	}
	return c.JSON(http.StatusOK, products)
}

// Insert with RETURNING
func createProductHandler(c echo.Context) error {
	var body ProductCreate
	if err := c.Bind(&body); err != nil {
		return c.JSON(http.StatusUnprocessableEntity, map[string]string{"detail": err.Error()})
	}

	var p ProductOut
	err := dbPool.QueryRow(c.Request().Context(),
		"INSERT INTO products (name, description, price, category_id, stock) "+
			"VALUES ($1, $2, $3, $4, $5) RETURNING id, name, price, stock",
		body.Name, body.Description, body.Price, body.CategoryID, body.Stock,
	).Scan(&p.ID, &p.Name, &p.Price, &p.Stock)

	if err != nil {
		return c.JSON(http.StatusInternalServerError, map[string]string{"detail": err.Error()})
	}
	return c.JSON(http.StatusCreated, p)
}

// Full update (PUT)
func updateProductHandler(c echo.Context) error {
	id, err := strconv.Atoi(c.Param("id"))
	if err != nil {
		return c.JSON(http.StatusBadRequest, map[string]string{"detail": "Invalid product ID"})
	}
	var body ProductCreate
	if err := c.Bind(&body); err != nil {
		return c.JSON(http.StatusUnprocessableEntity, map[string]string{"detail": err.Error()})
	}
	var p ProductOut
	err = dbPool.QueryRow(c.Request().Context(),
		"UPDATE products SET name=$1, description=$2, price=$3, category_id=$4, stock=$5 "+
			"WHERE id=$6 RETURNING id, name, price, stock",
		body.Name, body.Description, body.Price, body.CategoryID, body.Stock, id,
	).Scan(&p.ID, &p.Name, &p.Price, &p.Stock)
	if err != nil {
		return c.JSON(http.StatusNotFound, map[string]string{"detail": "Product not found"})
	}
	return c.JSON(http.StatusOK, p)
}

// Partial update (PATCH)
func patchProductHandler(c echo.Context) error {
	id, err := strconv.Atoi(c.Param("id"))
	if err != nil {
		return c.JSON(http.StatusBadRequest, map[string]string{"detail": "Invalid product ID"})
	}
	var body map[string]interface{}
	if err := json.NewDecoder(c.Request().Body).Decode(&body); err != nil {
		return c.JSON(http.StatusUnprocessableEntity, map[string]string{"detail": err.Error()})
	}
	setClauses := []string{}
	values := []interface{}{}
	idx := 1
	for _, key := range []string{"name", "description", "price", "category_id", "stock"} {
		if val, ok := body[key]; ok {
			setClauses = append(setClauses, fmt.Sprintf("%s=$%d", key, idx))
			values = append(values, val)
			idx++
		}
	}
	if len(setClauses) == 0 {
		return c.JSON(http.StatusBadRequest, map[string]string{"detail": "No fields to update"})
	}
	values = append(values, id)
	query := fmt.Sprintf("UPDATE products SET %s WHERE id=$%d RETURNING id, name, price, stock",
		joinStrings(setClauses, ", "), idx)
	var p ProductOut
	err = dbPool.QueryRow(c.Request().Context(), query, values...).Scan(&p.ID, &p.Name, &p.Price, &p.Stock)
	if err != nil {
		return c.JSON(http.StatusNotFound, map[string]string{"detail": "Product not found"})
	}
	return c.JSON(http.StatusOK, p)
}

// Delete product
func deleteProductHandler(c echo.Context) error {
	id, err := strconv.Atoi(c.Param("id"))
	if err != nil {
		return c.JSON(http.StatusBadRequest, map[string]string{"detail": "Invalid product ID"})
	}
	tag, err := dbPool.Exec(c.Request().Context(),
		"DELETE FROM products WHERE id=$1", id)
	if err != nil || tag.RowsAffected() == 0 {
		return c.JSON(http.StatusNotFound, map[string]string{"detail": "Product not found"})
	}
	return c.JSON(http.StatusOK, map[string]interface{}{"deleted": true, "id": id})
}

func joinStrings(strs []string, sep string) string {
	result := ""
	for i, s := range strs {
		if i > 0 {
			result += sep
		}
		result += s
	}
	return result
}

// Complex JOIN + GROUP BY aggregation
func categoryStatsHandler(c echo.Context) error {
	rows, err := dbPool.Query(c.Request().Context(),
		"SELECT c.id, c.name, COUNT(p.id) as product_count, "+
			"COALESCE(AVG(p.price), 0) as avg_price, "+
			"COALESCE(SUM(p.stock), 0) as total_stock "+
			"FROM categories c LEFT JOIN products p ON c.id = p.category_id "+
			"GROUP BY c.id, c.name ORDER BY c.name",
	)
	if err != nil {
		return c.JSON(http.StatusInternalServerError, map[string]string{"detail": err.Error()})
	}
	defer rows.Close()

	stats := make([]CategoryStats, 0)
	for rows.Next() {
		var s CategoryStats
		if err := rows.Scan(&s.ID, &s.Name, &s.ProductCount, &s.AvgPrice, &s.TotalStock); err != nil {
			continue
		}
		stats = append(stats, s)
	}
	return c.JSON(http.StatusOK, stats)
}

// Redis read-through cache
func getCachedProductHandler(c echo.Context) error {
	id, err := strconv.Atoi(c.Param("id"))
	if err != nil {
		return c.JSON(http.StatusBadRequest, map[string]string{"detail": "Invalid product ID"})
	}

	ctx := c.Request().Context()
	cacheKey := fmt.Sprintf("product:%d", id)

	// Check Redis cache
	cached, err := redisClient.Get(ctx, cacheKey).Result()
	if err == nil {
		// Cache hit -- return cached JSON
		var result map[string]interface{}
		if json.Unmarshal([]byte(cached), &result) == nil {
			return c.JSON(http.StatusOK, result)
		}
	}

	// Cache miss -- query DB
	var p ProductOut
	err = dbPool.QueryRow(ctx,
		"SELECT p.id, p.name, p.price, p.stock, c.name as category_name "+
			"FROM products p JOIN categories c ON p.category_id = c.id "+
			"WHERE p.id = $1", id,
	).Scan(&p.ID, &p.Name, &p.Price, &p.Stock, &p.CategoryName)

	if err != nil {
		return c.JSON(http.StatusNotFound, map[string]string{"detail": "Product not found"})
	}

	// Store in Redis with 60s TTL
	data, _ := json.Marshal(p)
	redisClient.Set(ctx, cacheKey, string(data), 60*time.Second)

	return c.JSON(http.StatusOK, p)
}

// Order with multi-table JOIN
func getOrderHandler(c echo.Context) error {
	id, err := strconv.Atoi(c.Param("id"))
	if err != nil {
		return c.JSON(http.StatusBadRequest, map[string]string{"detail": "Invalid order ID"})
	}

	ctx := c.Request().Context()

	// Fetch order
	var order OrderOut
	err = dbPool.QueryRow(ctx,
		"SELECT id, user_id, total, status, created_at FROM orders WHERE id = $1", id,
	).Scan(&order.ID, &order.UserID, &order.Total, &order.Status, &order.CreatedAt)

	if err != nil {
		return c.JSON(http.StatusNotFound, map[string]string{"detail": "Order not found"})
	}

	// Fetch order items
	rows, err := dbPool.Query(ctx,
		"SELECT oi.id, oi.order_id, oi.product_id, oi.quantity, oi.unit_price, "+
			"p.name as product_name "+
			"FROM order_items oi "+
			"JOIN products p ON oi.product_id = p.id "+
			"WHERE oi.order_id = $1", id,
	)
	if err != nil {
		return c.JSON(http.StatusInternalServerError, map[string]string{"detail": err.Error()})
	}
	defer rows.Close()

	items := make([]OrderItemOut, 0)
	for rows.Next() {
		var item OrderItemOut
		if err := rows.Scan(&item.ID, &item.OrderID, &item.ProductID, &item.Quantity, &item.UnitPrice, &item.ProductName); err != nil {
			continue
		}
		items = append(items, item)
	}

	return c.JSON(http.StatusOK, map[string]interface{}{
		"order": order,
		"items": items,
	})
}
