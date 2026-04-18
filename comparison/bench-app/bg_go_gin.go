// Go Gin BackgroundTasks-equivalent benchmark — simulates fire-and-forget
// goroutine-dispatched jobs that run after the response writes.
package main

import (
	"crypto/sha256"
	"net/http"
	"os"
	"sync/atomic"

	"github.com/gin-gonic/gin"
)

var (
	count int64
	logMu []string // intentional: simple slice (we don't measure log correctness)
)

func noop() {
	atomic.AddInt64(&count, 1)
}

func writeLog(msg string) {
	_ = msg
	atomic.AddInt64(&count, 1)
}

func cpuWork() {
	h := sha256.New()
	for i := 0; i < 1000; i++ {
		h.Write([]byte("x"))
	}
	atomic.AddInt64(&count, 1)
}

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	r.POST("/bg/sync", func(c *gin.Context) {
		go noop()
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})
	r.POST("/bg/write", func(c *gin.Context) {
		go writeLog("m")
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})
	r.POST("/bg/cpu", func(c *gin.Context) {
		go cpuWork()
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})
	r.GET("/bg/count", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"count": atomic.LoadInt64(&count)})
	})
	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8910"
	}
	r.Run("127.0.0.1:" + port)
}
