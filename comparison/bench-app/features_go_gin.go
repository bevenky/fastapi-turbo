package main

// Go Gin comparison server with middleware + custom error handler
// to match fastapi-turbo feature set.

import (
	"net/http"
	"os"

	"github.com/gin-gonic/gin"
)

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	// Custom middleware that adds a header (equivalent to @app.middleware("http"))
	r.Use(func(c *gin.Context) {
		c.Next()
		c.Header("X-MW", "y")
	})

	// Error handler equivalent — Gin uses c.AbortWithStatusJSON
	r.GET("/hello", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"message": "hello"})
	})

	r.GET("/hello-html", func(c *gin.Context) {
		c.Data(http.StatusOK, "text/html", []byte("<h1>hi</h1>"))
	})

	r.GET("/hello-cookie", func(c *gin.Context) {
		c.SetCookie("session", "abc123", 0, "/", "", false, false)
		c.JSON(http.StatusOK, gin.H{"message": "hello"})
	})

	r.GET("/err", func(c *gin.Context) {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"err": "test error"})
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8200"
	}
	r.Run("127.0.0.1:" + port)
}
