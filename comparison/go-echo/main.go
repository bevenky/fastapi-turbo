package main

import (
	"net/http"
	"os"

	"github.com/gorilla/websocket"
	"github.com/labstack/echo/v4"
)

var upgrader = websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}

func getDB() map[string]interface{} {
	return map[string]interface{}{"connected": true}
}

func getUser(db map[string]interface{}, auth string) map[string]interface{} {
	return map[string]interface{}{"name": "alice"}
}

type Item struct {
	Name  string  `json:"name" form:"name"`
	Price float64 `json:"price" form:"price"`
}

func main() {
	e := echo.New()
	e.HideBanner = true
	e.HidePort = true

	e.GET("/_ping", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]string{"ping": "pong"})
	})

	e.GET("/hello", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]string{"message": "hello"})
	})

	e.GET("/with-deps", func(c echo.Context) error {
		auth := c.Request().Header.Get("authorization")
		db := getDB()
		user := getUser(db, auth)
		return c.JSON(http.StatusOK, map[string]interface{}{"user": user["name"]})
	})

	e.POST("/items", func(c echo.Context) error {
		var item Item
		if err := c.Bind(&item); err != nil {
			return c.JSON(http.StatusUnprocessableEntity, map[string]string{"detail": err.Error()})
		}
		return c.JSON(http.StatusOK, map[string]interface{}{"name": item.Name, "price": item.Price, "created": true})
	})

	e.POST("/form-items", func(c echo.Context) error {
		var item Item
		if err := c.Bind(&item); err != nil {
			return c.JSON(http.StatusUnprocessableEntity, map[string]string{"detail": err.Error()})
		}
		return c.JSON(http.StatusOK, map[string]interface{}{"name": item.Name, "price": item.Price, "created": true})
	})

	// WebSocket echo endpoint
	e.GET("/ws", func(c echo.Context) error {
		conn, err := upgrader.Upgrade(c.Response(), c.Request(), nil)
		if err != nil {
			return err
		}
		defer conn.Close()
		for {
			mt, msg, err := conn.ReadMessage()
			if err != nil {
				break
			}
			conn.WriteMessage(mt, msg)
		}
		return nil
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8003"
	}
	e.Start("127.0.0.1:" + port)
}
