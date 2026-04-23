// File handling benchmark server — Go Gin.
//
// Endpoints:
//   POST /upload          — multipart/form-data, single file
//   GET  /download/:name  — c.File
//   GET  /static/:name    — r.Static mount
//   GET  /health
package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"

	"github.com/gin-contrib/gzip"
	"github.com/gin-gonic/gin"
)

func main() {
	gin.SetMode(gin.ReleaseMode)

	// Set up temp dir with the same test files as the fastapi-turbo version
	tmpDir, err := os.MkdirTemp("", "bench_files_go_")
	if err != nil {
		panic(err)
	}

	files := map[string][]byte{
		"small.txt":  []byte(""),
		"medium.bin": make([]byte, 64*1024),
		"large.bin":  make([]byte, 1024*1024),
		"style.css":  []byte(""),
	}
	smallText := []byte("hello world\n")
	cssText := []byte("body{color:red;}")
	for i := 0; i < 10; i++ {
		files["small.txt"] = append(files["small.txt"], smallText...)
	}
	for i := range files["medium.bin"] {
		files["medium.bin"][i] = 'x'
	}
	for i := range files["large.bin"] {
		files["large.bin"][i] = 'x'
	}
	for i := 0; i < 100; i++ {
		files["style.css"] = append(files["style.css"], cssText...)
	}
	for name, data := range files {
		if err := os.WriteFile(filepath.Join(tmpDir, name), data, 0644); err != nil {
			panic(err)
		}
	}

	r := gin.New()
	r.Use(gzip.Gzip(gzip.DefaultCompression))

	// Compressible JSON payload (~60 KB)
	type item struct {
		ID   int    `json:"id"`
		Name string `json:"name"`
		Desc string `json:"desc"`
	}
	bigItems := make([]item, 200)
	desc := "lorem ipsum dolor sit amet lorem ipsum dolor sit amet lorem ipsum dolor sit amet lorem ipsum dolor sit amet "
	for i := range bigItems {
		bigItems[i] = item{ID: i, Name: fmt.Sprintf("item-%d", i), Desc: desc}
	}

	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})

	r.GET("/json-big", func(c *gin.Context) {
		c.JSON(http.StatusOK, bigItems)
	})

	r.POST("/upload", func(c *gin.Context) {
		file, err := c.FormFile("file")
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"err": err.Error()})
			return
		}
		// Read the full content to match fastapi-turbo's behavior
		src, err := file.Open()
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"err": err.Error()})
			return
		}
		defer src.Close()
		data, err := io.ReadAll(src)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"err": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"filename": file.Filename, "size": len(data)})
	})

	r.GET("/download/:name", func(c *gin.Context) {
		name := c.Param("name")
		c.File(filepath.Join(tmpDir, name))
	})

	r.Static("/static", tmpDir)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8200"
	}
	r.Run("127.0.0.1:" + port)
}
