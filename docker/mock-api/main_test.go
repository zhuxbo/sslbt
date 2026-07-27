package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func TestIssueCertificateForCSRUsesCSRPublicKey(t *testing.T) {
	generateCACertPair("test.example.com")

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	csrDER, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: "local.example.com"},
	}, key)
	if err != nil {
		t.Fatal(err)
	}
	csrPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: csrDER})

	data, err := issueCertificateForCSR(string(csrPEM), 2001, "local.example.com")
	if err != nil {
		t.Fatalf("issueCertificateForCSR() error = %v", err)
	}
	if data.Status != "active" {
		t.Fatalf("status = %q, want active", data.Status)
	}
	if data.PrivateKey != "" {
		t.Fatal("local CSR response must not return a private key")
	}

	block, _ := pem.Decode([]byte(data.Cert))
	if block == nil {
		t.Fatal("issued certificate is not PEM")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	issuedKey, ok := cert.PublicKey.(*rsa.PublicKey)
	if !ok {
		t.Fatalf("issued public key type = %T, want *rsa.PublicKey", cert.PublicKey)
	}
	if issuedKey.N.Cmp(key.N) != 0 || issuedKey.E != key.E {
		t.Fatal("issued certificate public key does not match CSR public key")
	}
}

func TestCallbackRequestPreservesFailureMessage(t *testing.T) {
	raw := []byte(`{"order_id":1001,"status":"failure","deployed_at":"2026-07-18T00:00:00Z","message":"reload failed"}`)
	var req CallbackRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		t.Fatal(err)
	}
	if req.Message != "reload failed" {
		t.Fatalf("message = %q, want reload failed", req.Message)
	}
}

func TestHandleCallbackRejectsMessageOver500Characters(t *testing.T) {
	callbacksMutex.Lock()
	callbacks = nil
	callbacksMutex.Unlock()

	body := `{"order_id":1001,"status":"failure","deployed_at":"2026-07-18T00:00:00Z","message":"` + strings.Repeat("x", 501) + `"}`
	req := httptest.NewRequest(http.MethodPost, "/api/deploy/callback", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer test-token")
	recorder := httptest.NewRecorder()

	handleCallback(recorder, req)

	if recorder.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusUnprocessableEntity)
	}
	callbacksMutex.Lock()
	defer callbacksMutex.Unlock()
	if len(callbacks) != 0 {
		t.Fatalf("callbacks recorded = %d, want 0", len(callbacks))
	}
}

// ==============================================================================
// deploy-spec §2.2 / §2.3 契约回归
// ==============================================================================

func decodeQuery(t *testing.T, url string) (int, map[string]interface{}) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, url, nil)
	req.Header.Set("Authorization", "Bearer test-token")
	rec := httptest.NewRecorder()
	handleGetOrders(rec, req)
	var body map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是 JSON: %v (%s)", err, rec.Body.String())
	}
	return rec.Code, body
}

func errorCodeOf(t *testing.T, body map[string]interface{}) string {
	t.Helper()
	errs, ok := body["errors"].(map[string]interface{})
	if !ok {
		return ""
	}
	code, _ := errs["error_code"].(string)
	return code
}

func TestQueryRequiresOrderIDForm(t *testing.T) {
	setScenario("active")
	// 缺参、空串、域名、混合形态一律 invalid_order，且 HTTP 恒 200
	for _, url := range []string{
		"/api/deploy",
		"/api/deploy?order=",
		"/api/deploy?order=example.com",
		"/api/deploy?order=1001,example.com",
		"/api/deploy?order=abc",
	} {
		status, body := decodeQuery(t, url)
		if status != http.StatusOK {
			t.Fatalf("%s: HTTP = %d, want 200（业务失败恒 200）", url, status)
		}
		if got := errorCodeOf(t, body); got != "invalid_order" {
			t.Fatalf("%s: error_code = %q, want invalid_order", url, got)
		}
	}
}

func TestQueryResponseHasNoPaginationFields(t *testing.T) {
	setScenario("active")
	ordersMutex.Lock()
	orders[7001] = &OrderData{OrderID: 7001, Status: "active",
		Domains: "np.example.com", CommonName: "np.example.com"}
	ordersMutex.Unlock()

	_, body := decodeQuery(t, "/api/deploy?order=7001")
	data, ok := body["data"].(map[string]interface{})
	if !ok {
		t.Fatalf("data 不是对象: %v", body["data"])
	}
	for _, field := range []string{"total", "page", "page_size", "currentPage", "pageSize"} {
		if _, exists := data[field]; exists {
			t.Fatalf("响应不应包含分页字段 %q", field)
		}
	}
	if _, exists := data["renew_before_days"]; !exists {
		t.Fatal("响应缺少 renew_before_days")
	}
}

func TestSingleIDMissReturnsOrderNotFound(t *testing.T) {
	setScenario("active")
	status, body := decodeQuery(t, "/api/deploy?order=999999")
	if status != http.StatusOK {
		t.Fatalf("HTTP = %d, want 200", status)
	}
	if got := errorCodeOf(t, body); got != "order_not_found" {
		t.Fatalf("error_code = %q, want order_not_found", got)
	}
}

func TestBatchSilentlySkipsMissesAndNeverErrors(t *testing.T) {
	setScenario("active")
	ordersMutex.Lock()
	orders[7002] = &OrderData{OrderID: 7002, Status: "active",
		Domains: "b1.example.com", CommonName: "b1.example.com"}
	ordersMutex.Unlock()

	// 部分命中：只回命中的那条，不报错
	_, body := decodeQuery(t, "/api/deploy?order=7002,999998")
	data := body["data"].(map[string]interface{})
	items, _ := data["data"].([]interface{})
	if len(items) != 1 {
		t.Fatalf("命中条数 = %d, want 1", len(items))
	}
	if code := errorCodeOf(t, body); code != "" {
		t.Fatalf("部分命中不应报错，得到 %q", code)
	}

	// 全未命中：空数组而非报错
	_, body = decodeQuery(t, "/api/deploy?order=999998,999999")
	data = body["data"].(map[string]interface{})
	items, _ = data["data"].([]interface{})
	if len(items) != 0 {
		t.Fatalf("全未命中条数 = %d, want 0", len(items))
	}
	if code := errorCodeOf(t, body); code != "" {
		t.Fatalf("全未命中应返回空数组而非报错，得到 %q", code)
	}
}

func TestBatchOverLimitRejected(t *testing.T) {
	setScenario("active")
	ids := make([]string, maxBatchQueryItems+1)
	for i := range ids {
		ids[i] = strconv.Itoa(9000 + i)
	}
	_, body := decodeQuery(t, "/api/deploy?order="+strings.Join(ids, ","))
	if got := errorCodeOf(t, body); got != "invalid_order" {
		t.Fatalf("error_code = %q, want invalid_order", got)
	}
}

func TestInjectedErrorCodesUseCode0WithHTTP200(t *testing.T) {
	defer setScenario("active")
	for scenario, want := range injectedErrorCodes {
		setScenario(scenario)
		status, body := decodeQuery(t, "/api/deploy?order=7001")
		if status != http.StatusOK {
			t.Fatalf("%s: HTTP = %d, want 200（限流刻意不用 429）", scenario, status)
		}
		if code, _ := body["code"].(float64); code != 0 {
			t.Fatalf("%s: code = %v, want 0", scenario, body["code"])
		}
		if got := errorCodeOf(t, body); got != want {
			t.Fatalf("%s: error_code = %q, want %q", scenario, got, want)
		}
		if scenario == "rate_limited" {
			errs := body["errors"].(map[string]interface{})
			if _, ok := errs["retry_after"]; !ok {
				t.Fatal("rate_limited 必须携带 retry_after")
			}
		}
	}
}
