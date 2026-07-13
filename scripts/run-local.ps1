Param(
    [switch]$Build
)

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env создан из .env.example. Заполни секреты перед запуском."
}

if ($Build) {
    docker compose up --build
} else {
    docker compose up
}

