# Upstream fixtures

Responses in the shape returned by USDA FoodData Central (`/v1/foods/search`, `/v1/food/{id}`)
and Open Food Facts (`/api/v2/product/{code}`), trimmed to the fields Chomp reads. Tests replay
them through `httpx.MockTransport`, so CI needs no API key and makes no network calls.

The values were written by hand from FDC's and OFF's documented formats, because the
build environment could not reach either API. Re-record against the live APIs with a real
`FDC_API_KEY` if a field turns out to differ.
