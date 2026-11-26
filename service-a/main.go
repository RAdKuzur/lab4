package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"
	"os"
)

type Span struct {
	TraceID    string                 `json:"trace_id"`
	SpanID     string                 `json:"span_id"`
	ParentID   string                 `json:"parent_span_id,omitempty"`
	Name       string                 `json:"name"`
	Service    string                 `json:"service_name"`
	Kind       string                 `json:"kind"`
	StartTime  int64                  `json:"start_time_unix_nano"`
	EndTime    int64                  `json:"end_time_unix_nano"`
	Status     string                 `json:"status"`
	Attributes map[string]interface{} `json:"attributes"`
}

var (
    serviceBURL = getEnv(
        "SERVICE_B_URL",
        "http://service-b:8082/work",
    )

    collectorURL = getEnv(
        "COLLECTOR_URL",
        "http://trace-collector:4318/v1/traces",
    )

    httpClient = &http.Client{
        Timeout: 2 * time.Second,
    }
)

func getEnv(key, fallback string) string {
    value := os.Getenv(key)

    if value == "" {
        return fallback
    }

    return value
}

func genID(n int) string {
    b := make([]byte, n)
    if _, err := rand.Read(b); err != nil {
        log.Fatal(err)
    }
    return hex.EncodeToString(b)
}

func sendSpan(s Span) {
    body, err := json.Marshal(s)
    if err != nil {
        log.Printf("failed to marshal span: %v", err)
        return
    }

    resp, err := httpClient.Post(
        collectorURL,
        "application/json",
        bytes.NewBuffer(body),
    )

    if err != nil {
        log.Printf("failed to send span: %v", err)
        return
    }

    defer resp.Body.Close()

    if resp.StatusCode >= 400 {
        log.Printf("collector returned status: %d", resp.StatusCode)
    }
}

func handler(w http.ResponseWriter, r *http.Request) {
    if r.URL.Path == "/healthz" {
        w.WriteHeader(http.StatusOK)
        return
    }

    traceID := genID(16)
    spanID := genID(8)
    start := time.Now().UnixNano()

    req, err := http.NewRequest(
        http.MethodGet,
        serviceBURL,
        nil,
    )

    if err != nil {
        http.Error(w, "failed to create request", http.StatusInternalServerError)
        return
    }

    req.Header.Set(
        "traceparent",
        fmt.Sprintf("00-%s-%s-01", traceID, spanID),
    )

    status := "OK"

    resp, err := httpClient.Do(req)

    if err != nil {
        log.Printf("service-b call failed: %v", err)
        status = "ERROR"
    } else {
        resp.Body.Close()

        if resp.StatusCode >= 400 {
            status = "ERROR"
        }
    }

    end := time.Now().UnixNano()

    w.Header().Set("Content-Type", "application/json")

    json.NewEncoder(w).Encode(map[string]string{
        "trace_id": traceID,
        "status":   "ok",
    })

    go sendSpan(Span{
        TraceID:   traceID,
        SpanID:    spanID,
        Name:      "service-a-handler",
        Service:   "service-a",
        Kind:      "SERVER",
        StartTime: start,
        EndTime:   end,
        Status:    status,
        Attributes: map[string]interface{}{
            "http.method": r.Method,
            "http.path":   r.URL.Path,
        },
    })
}

func main() {
    http.HandleFunc("/", handler)

    log.Println("Service A listening on :8081")

    log.Fatal(http.ListenAndServe(":8081", nil))
}
