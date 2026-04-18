// Go Gin SSE bench — token-stream pattern matching vLLM/SGLang.
package main

import (
	"fmt"
	"net/http"
	"os"
	"strconv"

	"github.com/gin-gonic/gin"
)

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	r.GET("/stream", func(c *gin.Context) {
		n := 32
		if s := c.Query("n"); s != "" {
			if parsed, err := strconv.Atoi(s); err == nil && parsed > 0 {
				n = parsed
			}
		}

		c.Header("Content-Type", "text/event-stream")
		c.Header("Cache-Control", "no-cache")
		c.Header("Connection", "keep-alive")
		c.Status(http.StatusOK)

		w := c.Writer
		flusher, ok := w.(http.Flusher)
		for i := 0; i < n; i++ {
			fmt.Fprintf(w, "data: {\"idx\":%d,\"delta\":\"tok\"}\n\n", i)
			if ok {
				flusher.Flush()
			}
		}
		fmt.Fprintf(w, "data: [DONE]\n\n")
		if ok {
			flusher.Flush()
		}
	})

	r.GET("/health", func(c *gin.Context) {
		c.String(http.StatusOK, "ok")
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "19501"
	}
	r.Run("127.0.0.1:" + port)
}
