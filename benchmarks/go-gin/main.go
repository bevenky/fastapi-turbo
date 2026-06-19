// Go Gin bench app — mirrors benchmarks/_bench_app.py endpoint-for-endpoint
// so fastapi-turbo vs FastAPI vs Gin comparisons are apples-to-apples.
package main

import (
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
)

// ── simulated 2-level dependency chain (Gin has no native DI) ──
func getDB() map[string]interface{} {
	return map[string]interface{}{"connected": true}
}

func getUser(db map[string]interface{}, authorization string) map[string]interface{} {
	_ = authorization
	return map[string]interface{}{"name": "alice", "db": db["connected"]}
}

type Item struct {
	SKU  string   `json:"sku" binding:"required"`
	Qty  int      `json:"qty" binding:"required"`
	Tags []string `json:"tags"`
}

// ── streaming contract ──────────────────────────────────────────────
// Exactly 10 chunks. Each chunk is a fixed 64-byte line:
//
//	"chunk-{i}: " (9 bytes for single-digit i) + 54 '=' pad + "\n"
//
// Total body = 640 bytes. Content-Type: text/plain; charset=utf-8.
// NO artificial per-chunk delay — measures framework streaming OVERHEAD.
// MUST stay byte-identical across all 5 servers.
const streamChunks = 10

func streamChunk(i int) []byte {
	return []byte(fmt.Sprintf("chunk-%d: ", i) + strings.Repeat("=", 54) + "\n")
}

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	// Matches fastapi-turbo's built-in pure-Rust /_ping baseline.
	r.GET("/_ping", func(c *gin.Context) {
		c.Data(http.StatusOK, "application/json", []byte(`{"ping":"pong"}`))
	})

	r.GET("/hello", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"message": "hello"})
	})

	r.GET("/path/:id", func(c *gin.Context) {
		idStr := c.Param("id")
		id, err := strconv.Atoi(idStr)
		if err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": "int required"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"id": id, "squared": id * id})
	})

	r.GET("/headers", func(c *gin.Context) {
		ua := c.GetHeader("User-Agent")
		if ua == "" {
			ua = "unknown"
		}
		c.JSON(http.StatusOK, gin.H{"ua": ua, "req_id": c.GetHeader("X-Request-Id")})
	})

	r.GET("/with-deps", func(c *gin.Context) {
		auth := c.GetHeader("Authorization")
		if auth == "" {
			auth = "tok-demo"
		}
		db := getDB()
		user := getUser(db, auth)
		c.JSON(http.StatusOK, gin.H{"user": user["name"], "db": db["connected"]})
	})

	r.GET("/list", func(c *gin.Context) {
		items := make([]gin.H, 20)
		for i := 0; i < 20; i++ {
			items[i] = gin.H{"id": i, "name": fmt.Sprintf("item-%d", i)}
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})

	r.POST("/items", func(c *gin.Context) {
		var item Item
		if err := c.ShouldBindJSON(&item); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{
			"ok":        true,
			"sku":       item.SKU,
			"qty":       item.Qty,
			"tag_count": len(item.Tags),
		})
	})

	// 10-chunk stream, manual writer loop + flush per chunk. No delay.
	r.GET("/stream", func(c *gin.Context) {
		c.Header("Content-Type", "text/plain; charset=utf-8")
		c.Status(http.StatusOK)
		w := c.Writer
		for i := 0; i < streamChunks; i++ {
			_, _ = w.Write(streamChunk(i))
			w.Flush()
		}
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8099"
	}
	r.Run("127.0.0.1:" + port)
}
