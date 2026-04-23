// Mini E-commerce API -- Go Gin implementation.
//
// Exercises: CRUD, query params, path params, JSON body, auth middleware,
// CORS, form data login, WebSocket echo with timestamps.
package main

import (
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

// --------------------------------------------------------------------------
// Models
// --------------------------------------------------------------------------

type Item struct {
	ID          int      `json:"id"`
	Name        string   `json:"name" binding:"required"`
	Price       float64  `json:"price" binding:"required"`
	Description *string  `json:"description"`
}

type ItemCreate struct {
	Name        string  `json:"name" binding:"required"`
	Price       float64 `json:"price" binding:"required"`
	Description *string `json:"description"`
}

type ItemUpdate struct {
	Name        string  `json:"name" binding:"required"`
	Price       float64 `json:"price" binding:"required"`
	Description *string `json:"description"`
}

// --------------------------------------------------------------------------
// In-memory DB (pre-seeded)
// --------------------------------------------------------------------------

var (
	mu     sync.Mutex
	nextID = 4
	db     = map[int]Item{
		1: {ID: 1, Name: "Widget", Price: 9.99, Description: nil},
		2: {ID: 2, Name: "Gadget", Price: 19.99, Description: nil},
		3: {ID: 3, Name: "Doohickey", Price: 29.99, Description: nil},
	}
)

const secretToken = "secret-token-123"

var upgrader = websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}

// --------------------------------------------------------------------------
// Dependency simulation
// --------------------------------------------------------------------------

func getDB() map[int]Item {
	return db
}

func verifyToken(c *gin.Context) (string, bool) {
	auth := c.GetHeader("Authorization")
	if auth == "" || !strings.HasPrefix(auth, "Bearer ") {
		c.JSON(http.StatusUnauthorized, gin.H{"detail": "Missing or invalid token"})
		return "", false
	}
	token := auth[7:]
	if token != secretToken {
		c.JSON(http.StatusUnauthorized, gin.H{"detail": "Invalid token"})
		return "", false
	}
	return token, true
}

func getCurrentUser(c *gin.Context) (gin.H, bool) {
	_, ok := verifyToken(c)
	if !ok {
		return nil, false
	}
	_ = getDB() // simulate db dep
	return gin.H{"username": "demo_user", "email": "demo@example.com"}, true
}

// --------------------------------------------------------------------------
// main
// --------------------------------------------------------------------------

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	// CORS
	r.Use(cors.New(cors.Config{
		AllowOrigins:     []string{"*"},
		AllowMethods:     []string{"GET", "POST", "PUT", "DELETE", "OPTIONS"},
		AllowHeaders:     []string{"*"},
		AllowCredentials: true,
	}))

	// Health
	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})

	// List items
	r.GET("/items", func(c *gin.Context) {
		limitStr := c.DefaultQuery("limit", "10")
		offsetStr := c.DefaultQuery("offset", "0")
		limit, _ := strconv.Atoi(limitStr)
		offset, _ := strconv.Atoi(offsetStr)

		mu.Lock()
		items := make([]Item, 0, len(db))
		for _, item := range db {
			items = append(items, item)
		}
		mu.Unlock()

		// Sort by ID for deterministic output
		for i := 0; i < len(items); i++ {
			for j := i + 1; j < len(items); j++ {
				if items[i].ID > items[j].ID {
					items[i], items[j] = items[j], items[i]
				}
			}
		}

		end := offset + limit
		if offset >= len(items) {
			c.JSON(http.StatusOK, []Item{})
			return
		}
		if end > len(items) {
			end = len(items)
		}
		c.JSON(http.StatusOK, items[offset:end])
	})

	// Get item
	r.GET("/items/:id", func(c *gin.Context) {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"detail": "Invalid ID"})
			return
		}
		mu.Lock()
		item, ok := db[id]
		mu.Unlock()
		if !ok {
			c.JSON(http.StatusNotFound, gin.H{"detail": "Item not found"})
			return
		}
		c.JSON(http.StatusOK, item)
	})

	// Create item
	r.POST("/items", func(c *gin.Context) {
		var body ItemCreate
		if err := c.ShouldBindJSON(&body); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		mu.Lock()
		item := Item{ID: nextID, Name: body.Name, Price: body.Price, Description: body.Description}
		db[nextID] = item
		nextID++
		mu.Unlock()
		c.JSON(http.StatusCreated, item)
	})

	// Update item (PUT + PATCH share the same handler)
	updateHandler := func(c *gin.Context) {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"detail": "Invalid ID"})
			return
		}
		var body ItemUpdate
		if err := c.ShouldBindJSON(&body); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{"detail": err.Error()})
			return
		}
		mu.Lock()
		if _, ok := db[id]; !ok {
			mu.Unlock()
			c.JSON(http.StatusNotFound, gin.H{"detail": "Item not found"})
			return
		}
		item := Item{ID: id, Name: body.Name, Price: body.Price, Description: body.Description}
		db[id] = item
		mu.Unlock()
		c.JSON(http.StatusOK, item)
	}
	r.PUT("/items/:id", updateHandler)
	r.PATCH("/items/:id", updateHandler)

	// Delete item
	r.DELETE("/items/:id", func(c *gin.Context) {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"detail": "Invalid ID"})
			return
		}
		mu.Lock()
		delete(db, id)
		mu.Unlock()
		c.Status(http.StatusNoContent)
	})

	// Get current user (auth required)
	r.GET("/users/me", func(c *gin.Context) {
		user, ok := getCurrentUser(c)
		if !ok {
			return
		}
		c.JSON(http.StatusOK, user)
	})

	// Login (form data)
	r.POST("/login", func(c *gin.Context) {
		// Accept both form and JSON -- just return token
		c.JSON(http.StatusOK, gin.H{
			"access_token": secretToken,
			"token_type":   "bearer",
		})
	})

	// WebSocket chat
	r.GET("/ws/chat", func(c *gin.Context) {
		conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			_, message, err := conn.ReadMessage()
			if err != nil {
				break
			}
			var msg map[string]interface{}
			if err := json.Unmarshal(message, &msg); err != nil {
				continue
			}
			msg["server_ts"] = float64(time.Now().UnixNano()) / 1e9
			resp, _ := json.Marshal(msg)
			if err := conn.WriteMessage(websocket.TextMessage, resp); err != nil {
				break
			}
		}
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "19003"
	}
	r.Run("127.0.0.1:" + port)
}
