param(
    [Parameter(Mandatory = $true)]
    [string]$EmbeddingApiKey,

    [Parameter(Mandatory = $true)]
    [string]$EmbeddingBaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$EmbeddingModel,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 1000000)]
    [int]$EmbeddingDimension,

    [bool]$SendDimensions = $true,
    [string]$ApiKeyHeader = "Authorization",
    [string]$ApiKeyPrefix = "Bearer",
    [ValidateRange(1, 10000)]
    [int]$BatchSize = 10,
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 30,
    [ValidateRange(0, 20)]
    [int]$MaxRetries = 2,

    [ValidateRange(0.0, 1.0)]
    [double]$TextSearchVectorWeight = 0.80,
    [ValidateRange(0.0, 1.0)]
    [double]$TextSearchKeywordWeight = 0.20,
    [ValidateRange(0.0, 1.0)]
    [double]$TextSearchMinScore = 0.60,
    [ValidateRange(0.0, 1.0)]
    [double]$SqlSearchEmbeddingWeight = 0.50,
    [ValidateRange(0.0, 1.0)]
    [double]$SqlSearchKeywordWeight = 0.10,
    [ValidateRange(0.0, 1.0)]
    [double]$SqlSearchLogicWeight = 0.30,
    [ValidateRange(0.0, 1.0)]
    [double]$SqlSearchIdentifierWeight = 0.10,
    [ValidateRange(0.0, 1.0)]
    [double]$SqlSearchMinScore = 0.60
)

if ([string]::IsNullOrWhiteSpace($EmbeddingApiKey)) {
    throw "Embedding API key cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($EmbeddingBaseUrl)) {
    throw "Embedding base URL cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
    throw "Embedding model cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($ApiKeyHeader)) {
    throw "Embedding API key header cannot be empty."
}

$EnvFile = Join-Path $PSScriptRoot ".env"
$SendDimensionsText = $SendDimensions.ToString().ToLowerInvariant()
$Content = @(
    "EMBEDDING_API_KEY=$EmbeddingApiKey",
    "EMBEDDING_BASE_URL=$($EmbeddingBaseUrl.TrimEnd('/'))",
    "EMBEDDING_MODEL=$EmbeddingModel",
    "EMBEDDING_DIMENSION=$EmbeddingDimension",
    "EMBEDDING_SEND_DIMENSIONS=$SendDimensionsText",
    "EMBEDDING_API_KEY_HEADER=$ApiKeyHeader",
    "EMBEDDING_API_KEY_PREFIX=$ApiKeyPrefix",
    "EMBEDDING_BATCH_SIZE=$BatchSize",
    "EMBEDDING_TIMEOUT_SECONDS=$TimeoutSeconds",
    "EMBEDDING_MAX_RETRIES=$MaxRetries",
    "TEXT_SEARCH_VECTOR_WEIGHT=$TextSearchVectorWeight",
    "TEXT_SEARCH_KEYWORD_WEIGHT=$TextSearchKeywordWeight",
    "TEXT_SEARCH_DEFAULT_TOP_K=5",
    "TEXT_SEARCH_MAX_TOP_K=20",
    "TEXT_SEARCH_MIN_SCORE=$TextSearchMinScore",
    "SQL_SEARCH_EMBEDDING_WEIGHT=$SqlSearchEmbeddingWeight",
    "SQL_SEARCH_KEYWORD_WEIGHT=$SqlSearchKeywordWeight",
    "SQL_SEARCH_LOGIC_WEIGHT=$SqlSearchLogicWeight",
    "SQL_SEARCH_IDENTIFIER_WEIGHT=$SqlSearchIdentifierWeight",
    "SQL_SEARCH_MIN_SCORE=$SqlSearchMinScore"
)
Set-Content -LiteralPath $EnvFile -Value $Content -Encoding UTF8
Write-Host "Local .env configured for model '$EmbeddingModel', dimension $EmbeddingDimension. The key was not printed."
