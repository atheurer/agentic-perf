package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadDefaults(t *testing.T) {
	t.Setenv("AGENTIC_PERF_API_TOKEN", "")
	t.Setenv("AGENTIC_PERF_URL", "")
	t.Setenv("APTUI_CONFIG", "/nonexistent/config.toml")

	cfg := Load("", "")
	if cfg.URL != "http://localhost:8090" {
		t.Errorf("expected default URL, got %q", cfg.URL)
	}
}

func TestLoadFlagOverridesEnv(t *testing.T) {
	t.Setenv("AGENTIC_PERF_URL", "http://env:9999")
	t.Setenv("AGENTIC_PERF_API_TOKEN", "env-token")

	cfg := Load("http://flag:1234", "flag-token")
	if cfg.URL != "http://flag:1234" {
		t.Errorf("flag URL should override env, got %q", cfg.URL)
	}
	if cfg.Token != "flag-token" {
		t.Errorf("flag token should override env, got %q", cfg.Token)
	}
}

func TestLoadEnvOverridesFile(t *testing.T) {
	t.Setenv("AGENTIC_PERF_URL", "http://env:9999")
	t.Setenv("AGENTIC_PERF_API_TOKEN", "env-token")
	t.Setenv("APTUI_CONFIG", "/nonexistent/config.toml")

	cfg := Load("", "")
	if cfg.URL != "http://env:9999" {
		t.Errorf("env URL should win, got %q", cfg.URL)
	}
	if cfg.Token != "env-token" {
		t.Errorf("env token should win, got %q", cfg.Token)
	}
}

func TestLoadFromTOML(t *testing.T) {
	dir := t.TempDir()
	tomlPath := filepath.Join(dir, "client.toml")
	os.WriteFile(tomlPath, []byte(`url = "http://toml:5555"`+"\n"+`token = "toml-tok"`+"\n"), 0600)

	t.Setenv("APTUI_CONFIG", tomlPath)
	t.Setenv("AGENTIC_PERF_URL", "")
	t.Setenv("AGENTIC_PERF_API_TOKEN", "")

	cfg := Load("", "")
	if cfg.URL != "http://toml:5555" {
		t.Errorf("expected TOML URL, got %q", cfg.URL)
	}
	if cfg.Token != "toml-tok" {
		t.Errorf("expected TOML token, got %q", cfg.Token)
	}
}

func TestWriteAndReload(t *testing.T) {
	dir := t.TempDir()
	tomlPath := filepath.Join(dir, "client.toml")
	t.Setenv("APTUI_CONFIG", tomlPath)
	t.Setenv("AGENTIC_PERF_URL", "")
	t.Setenv("AGENTIC_PERF_API_TOKEN", "")

	cfg := Config{URL: "http://written:7777", Token: "written-tok"}
	if err := Write(cfg); err != nil {
		t.Fatalf("Write: %v", err)
	}

	info, err := os.Stat(tomlPath)
	if err != nil {
		t.Fatalf("Stat: %v", err)
	}
	if info.Mode().Perm() != 0600 {
		t.Errorf("expected 0600 perms, got %o", info.Mode().Perm())
	}

	loaded := Load("", "")
	if loaded.URL != "http://written:7777" {
		t.Errorf("reloaded URL: %q", loaded.URL)
	}
	if loaded.Token != "written-tok" {
		t.Errorf("reloaded token: %q", loaded.Token)
	}
}

func TestNeedsSetup(t *testing.T) {
	if !NeedsSetup(Config{URL: "http://x", Token: ""}) {
		t.Error("expected NeedsSetup=true with empty token")
	}
	if NeedsSetup(Config{URL: "http://x", Token: "tok"}) {
		t.Error("expected NeedsSetup=false with token set")
	}
}

func TestLoadSecretsFile(t *testing.T) {
	dir := t.TempDir()
	secretsDir := filepath.Join(dir, "secrets")
	os.MkdirAll(secretsDir, 0700)
	os.WriteFile(filepath.Join(secretsDir, "api-token"), []byte("secret-tok\n"), 0600)

	t.Setenv("AGENTIC_PERF_HOME", dir)
	t.Setenv("AGENTIC_PERF_API_TOKEN", "")
	t.Setenv("APTUI_CONFIG", "/nonexistent/config.toml")

	cfg := Load("", "")
	if cfg.Token != "secret-tok" {
		t.Errorf("expected secrets file token, got %q", cfg.Token)
	}
}
