package main

import (
	"net/http"
	"os"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}

// Simulated dependency injection (equivalent to Jamun's Depends)
func getDB() map[string]interface{} {
	return map[string]interface{}{"connected": true}
}

func getUser(db map[string]interface{}, authorization string) map[string]interface{} {
	return map[string]interface{}{"name": "alice"}
}

type Item struct {
	Name  string  `json:"name" form:"name" binding:"required"`
	Price float64 `json:"price" form:"price" binding:"required"`
}

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New() // No middleware for fair comparison

	// Baseline — equivalent to Jamun's /_ping
	r.GET("/_ping", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ping": "pong"})
	})

	// Simple GET — equivalent to Jamun's /hello
	r.GET("/hello", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"message": "hello"})
	})

	// GET with simulated DI — equivalent to Jamun's /with-deps
	r.GET("/with-deps", func(c *gin.Context) {
		authorization := c.GetHeader("authorization")
		db := getDB()
		user := getUser(db, authorization)
		c.JSON(http.StatusOK, gin.H{"user": user["name"]})
	})

	// POST with JSON body
	r.POST("/items", func(c *gin.Context) {
		var item Item
		if err := c.ShouldBindJSON(&item); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"name": item.Name, "price": item.Price, "created": true})
	})

	// POST with form data
	r.POST("/form-items", func(c *gin.Context) {
		var item Item
		if err := c.ShouldBind(&item); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"name": item.Name, "price": item.Price, "created": true})
	})

	// WebSocket echo endpoint
	r.GET("/ws", func(c *gin.Context) {
		conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			mt, message, err := conn.ReadMessage()
			if err != nil {
				break
			}
			conn.WriteMessage(mt, message)
		}
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8001"
	}
	r.Run("127.0.0.1:" + port)
}
