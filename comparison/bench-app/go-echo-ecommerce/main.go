// Mini E-commerce API -- Go Echo implementation (mirrors the Go Gin app).
package main

import (
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"
)

type Item struct {
	ID          int     `json:"id"`
	Name        string  `json:"name"`
	Price       float64 `json:"price"`
	Description *string `json:"description"`
}

type ItemCreate struct {
	Name        string  `json:"name" validate:"required"`
	Price       float64 `json:"price" validate:"required"`
	Description *string `json:"description"`
}

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

func verifyToken(c echo.Context) (string, bool) {
	auth := c.Request().Header.Get("Authorization")
	if auth == "" || !strings.HasPrefix(auth, "Bearer ") {
		c.JSON(http.StatusUnauthorized, echo.Map{"detail": "Missing or invalid token"})
		return "", false
	}
	token := auth[7:]
	if token != secretToken {
		c.JSON(http.StatusUnauthorized, echo.Map{"detail": "Invalid token"})
		return "", false
	}
	return token, true
}

func main() {
	e := echo.New()
	e.HideBanner = true
	e.HidePort = true
	e.Use(middleware.CORS())

	e.GET("/health", func(c echo.Context) error {
		return c.JSON(http.StatusOK, echo.Map{"status": "ok"})
	})

	e.GET("/items", func(c echo.Context) error {
		limit, _ := strconv.Atoi(c.QueryParam("limit"))
		if limit == 0 {
			limit = 10
		}
		offset, _ := strconv.Atoi(c.QueryParam("offset"))
		mu.Lock()
		items := make([]Item, 0, len(db))
		for _, item := range db {
			items = append(items, item)
		}
		mu.Unlock()
		for i := 0; i < len(items); i++ {
			for j := i + 1; j < len(items); j++ {
				if items[i].ID > items[j].ID {
					items[i], items[j] = items[j], items[i]
				}
			}
		}
		end := offset + limit
		if offset >= len(items) {
			return c.JSON(http.StatusOK, []Item{})
		}
		if end > len(items) {
			end = len(items)
		}
		return c.JSON(http.StatusOK, items[offset:end])
	})

	e.GET("/items/:id", func(c echo.Context) error {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			return c.JSON(http.StatusBadRequest, echo.Map{"detail": "Invalid ID"})
		}
		mu.Lock()
		item, ok := db[id]
		mu.Unlock()
		if !ok {
			return c.JSON(http.StatusNotFound, echo.Map{"detail": "Item not found"})
		}
		return c.JSON(http.StatusOK, item)
	})

	e.POST("/items", func(c echo.Context) error {
		var body ItemCreate
		if err := c.Bind(&body); err != nil {
			return c.JSON(http.StatusUnprocessableEntity, echo.Map{"detail": err.Error()})
		}
		mu.Lock()
		item := Item{ID: nextID, Name: body.Name, Price: body.Price, Description: body.Description}
		db[nextID] = item
		nextID++
		mu.Unlock()
		return c.JSON(http.StatusCreated, item)
	})

	updateHandler := func(c echo.Context) error {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			return c.JSON(http.StatusBadRequest, echo.Map{"detail": "Invalid ID"})
		}
		var body ItemCreate
		if err := c.Bind(&body); err != nil {
			return c.JSON(http.StatusUnprocessableEntity, echo.Map{"detail": err.Error()})
		}
		mu.Lock()
		if _, ok := db[id]; !ok {
			mu.Unlock()
			return c.JSON(http.StatusNotFound, echo.Map{"detail": "Item not found"})
		}
		item := Item{ID: id, Name: body.Name, Price: body.Price, Description: body.Description}
		db[id] = item
		mu.Unlock()
		return c.JSON(http.StatusOK, item)
	}
	e.PUT("/items/:id", updateHandler)
	e.PATCH("/items/:id", updateHandler)

	e.DELETE("/items/:id", func(c echo.Context) error {
		id, err := strconv.Atoi(c.Param("id"))
		if err != nil {
			return c.JSON(http.StatusBadRequest, echo.Map{"detail": "Invalid ID"})
		}
		mu.Lock()
		delete(db, id)
		mu.Unlock()
		return c.NoContent(http.StatusNoContent)
	})

	e.GET("/users/me", func(c echo.Context) error {
		if _, ok := verifyToken(c); !ok {
			return nil
		}
		return c.JSON(http.StatusOK, echo.Map{"username": "demo_user", "email": "demo@example.com"})
	})

	e.POST("/login", func(c echo.Context) error {
		return c.JSON(http.StatusOK, echo.Map{"access_token": secretToken, "token_type": "bearer"})
	})

	e.GET("/ws/chat", func(c echo.Context) error {
		conn, err := upgrader.Upgrade(c.Response(), c.Request(), nil)
		if err != nil {
			return err
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
		return nil
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "19005"
	}
	e.Start("127.0.0.1:" + port)
}
