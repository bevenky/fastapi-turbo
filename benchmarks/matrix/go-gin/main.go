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
	"github.com/gorilla/websocket"
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

// ── cross-framework matrix SQL (exact statements) ───────────────────
const (
	selOnePG  = "SELECT id, sku, name, qty FROM items WHERE id = 5"
	selListPG = "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT 10"
	insPG     = "INSERT INTO bench_writes (val) VALUES ('x')"
	updPG     = "UPDATE bench_writes SET val = 'y' WHERE id = 5"
	delPG     = "DELETE FROM bench_writes WHERE id = 999999999"

	rKey  = "bench:item"
	rWKey = "bench:wkey"
	rVal  = "bench-value"
)

// pgWrite runs one write statement inside an explicit transaction and
// commits or rolls back per the commit flag (so commit=false is repeatable).
func pgWrite(ctx context.Context, sql string, commit bool) error {
	tx, err := pgPool.Begin(ctx)
	if err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, sql); err != nil {
		_ = tx.Rollback(ctx)
		return err
	}
	if commit {
		return tx.Commit(ctx)
	}
	return tx.Rollback(ctx)
}

// commitFlag parses ?commit=true|false, defaulting to true.
func commitFlag(c *gin.Context) bool {
	if q := c.Query("commit"); q != "" {
		v, err := strconv.ParseBool(q)
		if err == nil {
			return v
		}
	}
	return true
}

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

// ── write-op response (ordered: "ok" then "committed") ──────────────
type WroteResp struct {
	OK        bool `json:"ok"`
	Committed bool `json:"committed"`
}

// ── redis pipeline/multi response (ordered: "ok" then "n") ──────────
type RedisPipeResp struct {
	OK bool `json:"ok"`
	N  int  `json:"n"`
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
	// CONTRACT: the payload is built PER REQUEST (all other frameworks do —
	// caching it at startup understated Gin's cost by ~40µs/req; audited).
	jsonLarge := func(c *gin.Context) { c.JSON(http.StatusOK, largeList()) }
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

	// ── cross-framework PG matrix: select_one ───────────────────────
	pgSelectOne := func(c *gin.Context) {
		var it PgItem
		row := pgPool.QueryRow(c.Request.Context(), selOnePG)
		if err := row.Scan(&it.ID, &it.SKU, &it.Name, &it.Qty); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, it)
	}
	r.GET("/pg/select_one/sync", pgSelectOne)
	r.GET("/pg/select_one/async", pgSelectOne)

	// ── cross-framework PG matrix: select_list ──────────────────────
	pgSelectList := func(c *gin.Context) {
		rows, err := pgPool.Query(c.Request.Context(), selListPG)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		defer rows.Close()
		items := make([]PgItem, 0, 10)
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
	r.GET("/pg/select_list/sync", pgSelectList)
	r.GET("/pg/select_list/async", pgSelectList)

	// ── cross-framework PG matrix: insert / update / delete ─────────
	pgWriteHandler := func(sql string) gin.HandlerFunc {
		return func(c *gin.Context) {
			commit := commitFlag(c)
			if err := pgWrite(c.Request.Context(), sql, commit); err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
				return
			}
			c.JSON(http.StatusOK, WroteResp{OK: true, Committed: commit})
		}
	}
	pgInsert := pgWriteHandler(insPG)
	r.POST("/pg/insert/sync", pgInsert)
	r.POST("/pg/insert/async", pgInsert)
	pgUpdate := pgWriteHandler(updPG)
	r.POST("/pg/update/sync", pgUpdate)
	r.POST("/pg/update/async", pgUpdate)
	pgDelete := pgWriteHandler(delPG)
	r.POST("/pg/delete/sync", pgDelete)
	r.POST("/pg/delete/async", pgDelete)

	// ── cross-framework Redis matrix (mode/op paths) ────────────────
	mRedisGet := func(c *gin.Context) {
		val, err := rdb.Get(c.Request.Context(), rKey).Result()
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.Data(http.StatusOK, "application/json", []byte(val))
	}
	r.GET("/redis/sync/get", mRedisGet)
	r.GET("/redis/async/get", mRedisGet)

	mRedisSet := func(c *gin.Context) {
		if err := rdb.Set(c.Request.Context(), rWKey, rVal, 0).Err(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, OKResp{OK: true})
	}
	r.POST("/redis/sync/set", mRedisSet)
	r.POST("/redis/async/set", mRedisSet)

	mRedisSetDurable := func(c *gin.Context) {
		ctx := c.Request.Context()
		if err := rdb.Set(ctx, rWKey, rVal, 0).Err(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		// force a local AOF fsync (0 replicas); ignore error if AOF is off.
		_ = rdb.Do(ctx, "WAITAOF", 1, 0, 100).Err()
		c.JSON(http.StatusOK, OKResp{OK: true})
	}
	r.POST("/redis/sync/set_durable", mRedisSetDurable)
	r.POST("/redis/async/set_durable", mRedisSetDurable)

	mRedisPipeline := func(c *gin.Context) {
		ctx := c.Request.Context()
		_, err := rdb.Pipelined(ctx, func(pipe redis.Pipeliner) error {
			for i := 0; i < 10; i++ {
				pipe.Set(ctx, fmt.Sprintf("%s:%d", rWKey, i), rVal, 0)
			}
			return nil
		})
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, RedisPipeResp{OK: true, N: 10})
	}
	r.POST("/redis/sync/pipeline", mRedisPipeline)
	r.POST("/redis/async/pipeline", mRedisPipeline)

	mRedisMulti := func(c *gin.Context) {
		ctx := c.Request.Context()
		_, err := rdb.TxPipelined(ctx, func(pipe redis.Pipeliner) error {
			for i := 0; i < 10; i++ {
				pipe.Set(ctx, fmt.Sprintf("%s:%d", rWKey, i), rVal, 0)
			}
			return nil
		})
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, RedisPipeResp{OK: true, N: 10})
	}
	r.POST("/redis/sync/multi", mRedisMulti)
	r.POST("/redis/async/multi", mRedisMulti)

	// ── WebSocket echo (contract: receive text -> echo SAME text back) ──
	upgrader := websocket.Upgrader{
		ReadBufferSize:  4096,
		WriteBufferSize: 4096,
		CheckOrigin:     func(r *http.Request) bool { return true },
	}
	r.GET("/ws", func(c *gin.Context) {
		conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			mt, msg, err := conn.ReadMessage()
			if err != nil {
				return // client closed (or error) — Close() completes the handshake
			}
			if err := conn.WriteMessage(mt, msg); err != nil {
				return
			}
		}
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8099"
	}
	if err := r.Run("127.0.0.1:" + port); err != nil {
		panic(err)
	}
}
