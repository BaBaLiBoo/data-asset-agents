param(
    [ValidateSet("up", "down", "test", "lint", "seed")]
    [string]$Command = "up"
)

switch ($Command) {
    "up"   { docker compose up --build -d }
    "down" { docker compose down }
    "test" { python -m pytest }
    "lint" { python -m ruff check . }
    "seed" { python data/seed/generate_seed.py }
}

