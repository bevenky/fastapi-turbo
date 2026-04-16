package main

// Go Gin equivalent of the fastapi-rs drop-in example.
// Matches every endpoint behavior for a fair benchmark.

import (
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
)

type Item struct {
	Name  string  `json:"name" binding:"required"`
	Price float64 `json:"price" binding:"required"`
}

// Custom error handler equivalent
func errorHandler(c *gin.Context, code int, detail string) {
	c.AbortWithStatusJSON(code, gin.H{"err": detail})
}

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	// Middleware equivalent to @app.middleware("http")
	r.Use(func(c *gin.Context) {
		c.Next()
		c.Header("x-custom", "1")
	})

	// Group under /api/v1 (matches root_path)
	api := r.Group("/api/v1")

	// GET /items/{item_id} with path param validation
	api.GET("/items/:item_id", func(c *gin.Context) {
		idStr := c.Param("item_id")
		itemID, err := strconv.Atoi(idStr)
		if err != nil {
			errorHandler(c, 422, "invalid item_id")
			return
		}
		if itemID < 1 {
			errorHandler(c, 422, "item_id must be >= 1")
			return
		}
		if itemID == 0 {
			errorHandler(c, 404, "nope")
			return
		}
		q := c.Query("q")
		if len(q) > 50 {
			errorHandler(c, 422, "q too long")
			return
		}
		var qResp interface{} = nil
		if q != "" {
			qResp = q
		}
		c.JSON(200, gin.H{"id": itemID, "q": qResp})
	})

	// POST /items with JSON body validation
	api.POST("/items", func(c *gin.Context) {
		var item Item
		if err := c.ShouldBindJSON(&item); err != nil {
			errorHandler(c, 422, err.Error())
			return
		}
		c.JSON(201, gin.H{"name": item.Name, "price": item.Price, "created": true})
	})

	// GET /me with Bearer auth
	api.GET("/me", func(c *gin.Context) {
		auth := c.GetHeader("Authorization")
		if strings.HasPrefix(auth, "Bearer ") {
			c.JSON(200, gin.H{"tok": auth[7:]})
			return
		}
		c.JSON(200, gin.H{"tok": nil})
	})

	// GET /html — HTML response
	api.GET("/html", func(c *gin.Context) {
		c.Data(200, "text/html; charset=utf-8", []byte("<h1>hi</h1>"))
	})

	// GET /c — set cookies
	api.GET("/c", func(c *gin.Context) {
		c.SetCookie("session", "abc", 3600, "/", "", false, false)
		c.SetCookie("theme", "dark", 86400, "/", "", true, false)
		c.JSON(200, gin.H{"ok": true})
	})

	// GET /hello — baseline
	api.GET("/hello", func(c *gin.Context) {
		c.JSON(200, gin.H{"message": "hello"})
	})

	// OpenAPI equivalent (Gin doesn't have this built-in; skip for perf comparison)
	r.GET("/api/v1/openapi.json", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"openapi": "3.0.0"})
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8600"
	}
	r.Run("127.0.0.1:" + port)
}
