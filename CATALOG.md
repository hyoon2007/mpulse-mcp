# mPulse Query API — Reference Catalog

`src/mpulse_mcp/catalog.json` + `src/mpulse_mcp/catalog.py`.

목적: 유효한 metric·timer·dimension 이름과 동작 규칙을 미리 확정해, 에이전트가 매번 시행착오로 재탐색하지 않게 한다. 출처: Akamai TechDocs 엔드포인트 레퍼런스 페이지 + Akamai cli-mpulse + 라이브 API(app=app-a) 실증.

## 구성

- **`catalog.json`** — 권위 있는 enum/규칙 데이터(머신 판). hatchling 휠에 자동 포함됨.
- **`catalog.py`** — fail-safe 로더. JSON이 없거나 깨져도 `{}`를 반환하므로 서버 동작에 영향 없음.
- **`server.py`의 `describe_query`** — 응답에 `catalog.enrich_describe(query_type)` 결과를 병합. 이제 query type별로 아래를 실어준다:
  - `valid_metrics` (엔드포인트에 맞는 이름 목록)
  - `valid_timers`
  - `valid_dimensions`
  - `dimension_value_examples`, `gotchas`, `metric_name_differs_by_endpoint`

즉 `describe_query("timers-metrics")` 한 번이면 metric 98종·timer 24종과 함정 목록까지 나온다.

## 가장 중요한 함정 — 같은 지표, 엔드포인트마다 다른 이름

| 개념 | `timers-metrics` (CamelCase) | `metrics-by-dimension` (snake_case) |
|---|---|---|
| 페이지당 요청 수 | `TotalRequestCount` * | `asset_requests_per_page` |
| Decoded Body Size | `TotalDecodedBodySize` | `asset_decoded_body_size` |
| Transfer Size | `TotalTransferSize` | `asset_transfer_size` |

`*` `TotalRequestCount`는 timers-metrics 문서 enum엔 없지만(Total* 그룹 8개 = 문서 "97개"와 일치) 라이브에선 정상 동작(실증: 6월 p75=142, 7월=151). `catalog.json`의 `verified_live_extra`로 별도 수록.

## enum 요약 (전체는 catalog.json)

- **timers-metrics `metric`** — CamelCase 97종(+TotalRequestCount). 접두사 `Bcn/Css/Font/Html/Img/Js/Other/Page/Total/Xhr` × 접미사 `RequestCount/TransferSize/DecodedBodySize/CompressionRatio/…`.
- **metrics-by-dimension `metric`** — snake_case 82종(`asset_*`, `css_*`, `js_*`, `image_*`, `font_*`, `html_*`, `xhr_*`, `other_*`, `page_*`, `beacon_*` 계열), 콤마구분 다중.
- **timer** — 24종(`PageLoad` 기본 … `LargestContentfulPaint`, `TotalBlockingTime`, `CustomTimer[0-9]`).
- **dimension-values `dimension`** — 24종(underscore). `connection_type` 없음 → dimension-values에서 `connection-type` 400.
- **metrics-by-dimension split `dimension`** — 33종(underscore).
- **beacon-type** 9종(`page view`, `xhr`, `spa_hard`, `spa`, …). **device-type** `Mobile/Desktop/Tablet`(+라이브 `(No Value)`). **bandwith-block** 0–6/.NONE.

## 동작 규칙(gotchas)

- 잘못된 `timer`/`metric` → 에러 없이 **PageLoad로 조용히 폴백**. 응답 series `id`가 요청 이름과 같은지 확인.
- 잘못된 `custom-timer` → **400**.
- `metric`을 리스트로 넘기면 MCP에서 무시됨 → 호출당 1개 문자열.
- **`latest` = 구간 전체 집계값**. percentile metric을 `Between`으로 조회하면 `latest`가 구간 전체 백분위 → 월 p75/p50은 일별 history 평균이 아니라 `latest`를 읽기.
- `Between`의 `date-end`는 **exclusive**(문서는 "greater than"이라고만 함). 긴 구간은 버킷 자동 coarsening.
- rate limit: 동시 3 / 분당 100 / 시간당 10,000 / 일 50,000.

## bandwidth 필터 파라미터 철자 — `bandwith-block` (라이브 확정, 코드 수정 완료)

mPulse의 실제 wire 파라미터는 **`bandwith-block`**(d 빠진 오타)이며, 정상 철자 `bandwidth-block`은 **동작하지 않습니다**(앱 소유자 라이브 검증). 이에 맞춰 코드도 수정했습니다:
- `query_types.DRILLDOWN_PARAMS`: `bandwidth-block` → `bandwith-block`
- `server._DRILLDOWN_ARG_TO_WIRE`: 친화적 인자명 `bandwidth_block`은 유지하되 wire 값을 `bandwith-block`으로 매핑

## Custom dimension 사용법

고객사가 정의한 custom dimension은 built-in enum에 없으며, **조회 API가 없어 사용자가 이름을 정확히 입력**해야 합니다.

- **필터**로 사용: `custom-dimension-<label>=<value>` — 예: `branch=uk`로 필터링하려면 `custom-dimension-branch=uk`.
- **분해(split)**로 사용: metrics-by-dimension에서 `dimension=<custom_name>` — 예: branch별 metric을 보려면 `dimension=branch`.
- `<label>`은 custom dimension 이름을 소문자로, 공백은 `_`로. 값이 없는 beacon은 `.NONE`으로 매칭.

즉 `metrics_by_dimension__dimension_split_enum`의 33종은 built-in일 뿐이고, custom dimension 이름도 `dimension` 값으로 그대로 유효합니다.

## 아직 못 채운 부분 — custom metric/timer/custom-dimension

위 enum은 전부 **built-in**입니다. 이 앱 고유의 custom 항목은 공개 문서에 없고 다음에서만 나옵니다:
- Repository/Objects API: `getRepositoryDomain(token, appName=...)["custom_metrics"]`
- 또는 대시보드 report-builder의 `<option>` 목록.

확보되면 `catalog.json`에 `custom_metrics_by_app` 같은 섹션으로 병합하고, `catalog.enrich_describe`에서 앱별로 덧붙이면 됩니다.

## 유지보수

- enum 갱신은 `catalog.json`만 수정하면 됨(코드 불변). 로더는 `@lru_cache`라 프로세스 재시작 시 반영.
- 검증: `python -c "from mpulse_mcp import catalog; print(len(catalog.enrich_describe('timers-metrics')['valid_metrics']))"` → 98.
