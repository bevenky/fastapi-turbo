// Go Gin matrix bench app — mirrors benchmarks/matrix/app.py (the CONTRACT)
// endpoint-for-endpoint with byte-identical JSON/XML/stream responses.
//
// Byte-parity rules:
//   - JSON uses ORDERED structs with json tags (NEVER gin.H / maps, which
//     encoding/json sorts alphabetically and would reorder keys).
//   - JSON bodies use only ints + strings (no floats).
//   - XML and redis-GET responses are written verbatim (raw bytes).
//   - Streaming: 10 fixed 64-byte lines, flush per chunk.
package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

// ── connection targets (shared by all servers) ──────────────────────
const (
	pgDSN     = "postgres://venky@127.0.0.1:5432/fastapi_turbo_bench"
	redisAddr = "127.0.0.1:6379"
)

var (
	pgPool *pgxpool.Pool
	rdb    *redis.Client
)

// ── request bodies ──────────────────────────────────────────────────
type ItemBody struct {
	SKU  string   `json:"sku"`
	Qty  int      `json:"qty"`
	Tags []string `json:"tags"`
}

type PatchBody struct {
	Qty int `json:"qty"`
}

// ── ordered response structs (json tag order == contract key order) ──
type MessageResp struct {
	Message string `json:"message"`
}

type ListEntry struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

type LargeListResp struct {
	Items []ListEntry `json:"items"`
}

type CreateResp struct {
	OK       bool   `json:"ok"`
	SKU      string `json:"sku"`
	Qty      int    `json:"qty"`
	TagCount int    `json:"tag_count"`
}

type PutResp struct {
	OK       bool   `json:"ok"`
	ID       int    `json:"id"`
	SKU      string `json:"sku"`
	Qty      int    `json:"qty"`
	TagCount int    `json:"tag_count"`
}

type PatchResp struct {
	OK  bool `json:"ok"`
	ID  int  `json:"id"`
	Qty int  `json:"qty"`
}

type DeleteResp struct {
	OK      bool `json:"ok"`
	Deleted int  `json:"deleted"`
}

type OKResp struct {
	OK bool `json:"ok"`
}

type PingResp struct {
	Ping string `json:"ping"`
}

type PgItem struct {
	ID   int    `json:"id"`
	SKU  string `json:"sku"`
	Name string `json:"name"`
	Qty  int    `json:"qty"`
}

type PgItemsResp struct {
	Items []PgItem `json:"items"`
}

// ── large JSON payload (~28791 bytes), built once ───────────────────
func largeList() LargeListResp {
	items := make([]ListEntry, 1000)
	for i := 0; i < 1000; i++ {
		items[i] = ListEntry{ID: i, Name: fmt.Sprintf("item-%d", i)}
	}
	return LargeListResp{Items: items}
}

// ── XML payloads (built once) ───────────────────────────────────────
var xmlSmall = []byte(`<?xml version="1.0" encoding="UTF-8"?><item><id>1</id><name>item-1</name></item>`)

func buildXMLLarge() []byte {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?><items>`)
	for i := 0; i < 1000; i++ {
		b.WriteString(fmt.Sprintf("<item><id>%d</id><name>item-%d</name></item>", i, i))
	}
	b.WriteString("</items>")
	return []byte(b.String())
}

var xmlLarge = buildXMLLarge()

// ── streaming contract: 10 fixed 64-byte lines ──────────────────────
const (
	streamChunks = 10
	streamCT     = "text/plain; charset=utf-8"
)

func streamChunk(i int) []byte {
	return []byte(fmt.Sprintf("chunk-%d: ", i) + strings.Repeat("=", 54) + "\n")
}

func writeStream(c *gin.Context) {
	c.Header("Content-Type", streamCT)
	c.Status(http.StatusOK)
	w := c.Writer
	for i := 0; i < streamChunks; i++ {
		_, _ = w.Write(streamChunk(i))
		w.Flush()
	}
}

func main() {
	ctx := context.Background()

	// Postgres pool: min 4, max 8.
	cfg, err := pgxpool.ParseConfig(pgDSN)
	if err != nil {
		panic(err)
	}
	cfg.MinConns = 4
	cfg.MaxConns = 8
	pgPool, err = pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		panic(err)
	}
	defer pgPool.Close()
	if err := pgPool.Ping(ctx); err != nil {
		panic(fmt.Sprintf("pg ping failed: %v", err))
	}

	// Redis client.
	rdb = redis.NewClient(&redis.Options{Addr: redisAddr})
	defer rdb.Close()
	if err := rdb.Ping(ctx).Err(); err != nil {
		panic(fmt.Sprintf("redis ping failed: %v", err))
	}

	largeJSON := largeList()

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	// Static baseline — matches fastapi-turbo's pure-Rust /_ping.
	r.GET("/_ping", func(c *gin.Context) {
		c.Data(http.StatusOK, "application/json", []byte(`{"ping":"pong"}`))
	})

	// ── plain JSON: simple ──────────────────────────────────────────
	hello := func(c *gin.Context) { c.JSON(http.StatusOK, MessageResp{Message: "hello"}) }
	r.GET("/hello", hello)
	r.GET("/async/hello", hello)

	// ── plain JSON: large ───────────────────────────────────────────
	jsonLarge := func(c *gin.Context) { c.JSON(http.StatusOK, largeJSON) }
	r.GET("/json/large", jsonLarge)
	r.GET("/async/json/large", jsonLarge)

	// ── POST /items, /async/items ───────────────────────────────────
	createItem := func(c *gin.Context) {
		var item ItemBody
		if err := c.ShouldBindJSON(&item); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, CreateResp{
			OK: true, SKU: item.SKU, Qty: item.Qty, TagCount: len(item.Tags),
		})
	}
	r.POST("/items", createItem)
	r.POST("/async/items", createItem)

	// ── PUT /items/{id} ─────────────────────────────────────────────
	r.PUT("/items/:id", func(c *gin.Context) {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": "int required"})
			return
		}
		var item ItemBody
		if err := c.ShouldBindJSON(&item); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, PutResp{
			OK: true, ID: id, SKU: item.SKU, Qty: item.Qty, TagCount: len(item.Tags),
		})
	})

	// ── PATCH /items/{id} ───────────────────────────────────────────
	r.PATCH("/items/:id", func(c *gin.Context) {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": "int required"})
			return
		}
		var patch PatchBody
		if err := c.ShouldBindJSON(&patch); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, PatchResp{OK: true, ID: id, Qty: patch.Qty})
	})

	// ── DELETE /items/{id} ──────────────────────────────────────────
	r.DELETE("/items/:id", func(c *gin.Context) {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": "int required"})
			return
		}
		c.JSON(http.StatusOK, DeleteResp{OK: true, Deleted: id})
	})

	// ── XML ─────────────────────────────────────────────────────────
	r.GET("/xml/small", func(c *gin.Context) {
		c.Data(http.StatusOK, "application/xml", xmlSmall)
	})
	r.GET("/xml/large", func(c *gin.Context) {
		c.Data(http.StatusOK, "application/xml", xmlLarge)
	})

	// ── streaming (sync/async/await all identical here) ─────────────
	r.GET("/stream-sync", writeStream)
	r.GET("/stream-async", writeStream)
	r.GET("/stream-await", writeStream)

	// ── Redis GET (verbatim value) ──────────────────────────────────
	redisGet := func(c *gin.Context) {
		val, err := rdb.Get(c.Request.Context(), "bench:item").Result()
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.Data(http.StatusOK, "application/json", []byte(val))
	}
	r.GET("/redis/get/sync", redisGet)
	r.GET("/redis/get/async", redisGet)

	// ── Redis SET ───────────────────────────────────────────────────
	redisSet := func(c *gin.Context) {
		if err := rdb.Set(c.Request.Context(), "bench:wkey", "bench-value", 0).Err(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, OKResp{OK: true})
	}
	r.POST("/redis/set/sync", redisSet)
	r.POST("/redis/set/async", redisSet)

	// ── Postgres: item by id ────────────────────────────────────────
	pgItem := func(c *gin.Context) {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": "int required"})
			return
		}
		var it PgItem
		row := pgPool.QueryRow(c.Request.Context(),
			"SELECT id, sku, name, qty FROM items WHERE id = $1", id)
		if err := row.Scan(&it.ID, &it.SKU, &it.Name, &it.Qty); err != nil {
			c.Status(http.StatusNotFound)
			return
		}
		c.JSON(http.StatusOK, it)
	}
	r.GET("/pg/item/:id/sync", pgItem)
	r.GET("/pg/item/:id/async", pgItem)

	// ── Postgres: item list ─────────────────────────────────────────
	pgItems := func(c *gin.Context) {
		limit := 10
		if q := c.Query("limit"); q != "" {
			if v, err := strconv.Atoi(q); err == nil {
				limit = v
			}
		}
		rows, err := pgPool.Query(c.Request.Context(),
			"SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1", limit)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		defer rows.Close()
		items := make([]PgItem, 0, limit)
		for rows.Next() {
			var it PgItem
			if err := rows.Scan(&it.ID, &it.SKU, &it.Name, &it.Qty); err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
				return
			}
			items = append(items, it)
		}
		if err := rows.Err(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, PgItemsResp{Items: items})
	}
	r.GET("/pg/items/sync", pgItems)
	r.GET("/pg/items/async", pgItems)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8099"
	}
	if err := r.Run("127.0.0.1:" + port); err != nil {
		panic(err)
	}
}
