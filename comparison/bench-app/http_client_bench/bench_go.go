package main

// Go HTTP client parallel benchmark.
// Makes N parallel GET requests using goroutines and waits for all to complete.
// Reports p50, min, p99 across the iteration loop.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"
)

func main() {
	if len(os.Args) < 4 {
		fmt.Println("Usage: bench_go <url> <parallel_count> <iterations>")
		os.Exit(1)
	}
	url := os.Args[1]
	parallel, _ := strconv.Atoi(os.Args[2])
	iters, _ := strconv.Atoi(os.Args[3])
	warmup := 500

	// Shared HTTP client with connection pool
	client := &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        200,
			MaxIdleConnsPerHost: 200,
			MaxConnsPerHost:     200,
			IdleConnTimeout:     30 * time.Second,
			DisableCompression:  false,
		},
		Timeout: 30 * time.Second,
	}

	doRequest := func() ([]byte, error) {
		resp, err := client.Get(url)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		return io.ReadAll(resp.Body)
	}

	doParallel := func(n int) {
		var wg sync.WaitGroup
		wg.Add(n)
		for i := 0; i < n; i++ {
			go func() {
				defer wg.Done()
				body, err := doRequest()
				if err != nil {
					return
				}
				_ = body
			}()
		}
		wg.Wait()
	}

	// Warmup
	for i := 0; i < warmup; i++ {
		doParallel(parallel)
	}

	// Benchmark
	times := make([]float64, iters)
	for i := 0; i < iters; i++ {
		t0 := time.Now()
		doParallel(parallel)
		times[i] = float64(time.Since(t0).Microseconds())
	}

	sort.Float64s(times)
	p50 := times[iters/2]
	p99 := times[int(float64(iters)*0.99)]
	mn := times[0]

	result := map[string]interface{}{
		"parallel":    parallel,
		"iterations":  iters,
		"p50_us":      p50,
		"p99_us":      p99,
		"min_us":      mn,
		"per_req_us":  p50 / float64(parallel),
	}
	out, _ := json.Marshal(result)
	fmt.Println(string(out))
}
