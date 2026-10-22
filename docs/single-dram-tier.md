# KVCR 단일 DRAM tier (vLLM + KVCR Guard)

vLLM CPU offload tier와 KVCR DRAM을 하나로 합치는 작업입니다. 지금까지 한 것과 B200에서 검증할 것을 정리합니다.

- 브랜치: `feat/framework-managed-pool` (이 레포)
- vLLM 변경: `integrations/vllm/single-dram-tier.patch`
  - KVCR이 고정한 vLLM 커밋 `08567505b09324f891797f4a13c1bd73fc75a827` 기준 패치입니다.
  - `Dockerfile.quick-start`가 이미지 빌드 때 자동으로 적용합니다.
- 상태: 로컬(macOS)에서 코드 작성과 로직 단위 테스트까지 끝났습니다. **B200 검증 전**입니다.

## 1. 왜 바꾸나

지금은 블록 하나를 저장할 때 DRAM에서 DRAM으로 복사가 한 번 더 일어나고, 같은 블록이 DRAM에 두 벌 남습니다.

```
지금   HBM ─① GPU→CPU→ vLLM DRAM ─② deposit(DRAM→DRAM 복사)→ KVCR DRAM ─③ eviction→ SSD
              (/dev/shm/vllm_offload_*)                        (Guard pool)

변경   HBM ─① GPU→CPU→ Guard pool (단일 DRAM) ─② 같은 slot에서→ SSD
                        └ slot 제어: vLLM / key→slot 기록: KVCR journal / 메모리 소유: KVCR service
```

| 항목 | vLLM | KVCR (vLLM 프로세스 안) | KVCR service (Guard) |
| --- | --- | --- | --- |
| 메모리 존재와 수명 | - | - | 소유 (pool 파일, fencing) |
| slot 할당, eviction, GPU↔CPU 복사 | 담당 | 하지 않음 | - |
| key→slot 기록 | 자체 CPU manager | journal에 기록 | mirror 보관 |
| eviction 허락 | 요청 | 읽는 중이거나 SSD에 쓰는 중이면 거부 | - |
| peer 서빙 | - | 같은 slot에서 직접 | vLLM이 죽은 동안 대신 |
| SSD(G3) | - | eviction 때 같은 slot에서 spill | - |

vLLM의 메모리 레이아웃은 그대로입니다.
- 행 구조 `num_chunks × kv_bytes_per_chunk`와, 행 안에서 rank별로 고정된 영역(`[r×cpu_page_size, (r+1)×cpu_page_size)`)을 바꾸지 않습니다.
- 영역의 시작 주소만 Guard pool로 바뀌고, **chunk i가 곧 KVCR slot i**입니다.

## 2. 동작

### 저장
1. vLLM이 GPU→pool 복사를 끝내면 cascade로 `commit_framework_slots({key: [("", chunk)]})`를 부릅니다.
2. KVCR은 복사하지 않고, key가 어느 slot에 있는지만 기록합니다. 이때 journal에 ADD가 남습니다.

### eviction
1. vLLM이 victim을 고르면 `try_retire_framework_slots`로 KVCR에 허락을 받습니다.
   - **True:** journal에 REMOVE가 기록된 뒤이므로 slot을 다시 써도 됩니다.
   - **False:** peer 전송이나 SSD 쓰기가 진행 중입니다. vLLM은 그 블록을 캐시에서 빼되 slot은 그대로 두고, 다른 victim을 고릅니다. 다음 store 때 다시 물어보고, 허락이 나면 slot을 해제합니다.
2. 기본 정책(`G3LRUPolicy`)은 예전과 같이 eviction될 때 SSD로 spill합니다.
   - 첫 retire 요청에서 SSD 쓰기를 시작하고 False를 돌려줍니다.
   - 쓰기가 끝난 뒤 다시 요청하면 True를 돌려주고, 블록은 SSD에 남습니다.
3. 캐시에서는 빠졌지만 slot은 아직 남아 있는 블록을 다시 요청하면, 복사 없이 캐시로 되돌립니다.

### 읽기
- SSD나 peer에서 블록을 받아 vLLM chunk에 채우는 promotion은, 데이터가 도착한 뒤에 commit합니다.
- KVCR이 LOCAL_G2 hit을 알려도 MISS로 처리합니다. 이미 vLLM chunk에 있는 블록이라 같은 pool 안에서 복사할 이유가 없기 때문입니다.

### 크래시와 재시작
1. vLLM이 죽으면 Guard가 pidfd로 확인하고 journal을 끝까지 읽습니다. 그다음 promote해서 G2를 peer에 서빙합니다.
2. vLLM이 재시작해서 claim하면 Guard가 handback합니다.
3. vLLM은 `local_dram_residency()`로 받은 {key: slot}을 CPU manager에 복원하고, 그 블록들은 바로 hit합니다.
   - 로그: `Primary tier lives in the KVCR service pool at 0x...; N chunk(s) recovered`

### 순서 규칙 (crash safety)
- **ADD:** 바이트 쓰기가 끝난 뒤에만 기록합니다.
- **REMOVE:** 그 slot을 덮어쓰기 전에 기록합니다.

이 순서를 지키기 때문에, 크래시가 어느 시점에 나도 journal이 "key는 K인데 내용은 다른 블록"인 slot을 가리키지 않습니다. 이 순서는 테스트로 고정해 두었습니다.

## 3. 변경 내용

### kvcr (이 레포)
| 파일 | 내용 |
| --- | --- |
| `config.py` | `KVCRConfig.framework_managed_slots` |
| `local_dram.py` | framework 모드: KVCR이 slot을 할당하거나 evict하지 않음. `commit_framework_slots`, `try_retire_framework_slots`(정책에 eviction을 묻고 spill), `residency`, `pools` |
| `local_disk.py` | framework 모드에서는 spill이 끝나도 G2를 자동 회수하지 않고 "spill 끝남" 표시만 남김 (slot 회수는 vLLM이 함) |
| `core.py`, `api.py` | 공개 API: `local_dram_pools()`, `local_dram_residency()`, `commit_framework_slots()`, `try_retire_framework_slots()`. framework 모드에서는 framework region이 pool 자체를 가리킴 |
| `progress.py` | 같은 메모리 범위는 NIXL에 한 번만 등록 |

journal record 형식은 바꾸지 않았습니다. 기존과 같은 `_RECORD_BLOCK`과 payload를 쓰기 때문에, Guard 쪽 코드(mirror, promote, handback)는 수정하지 않았습니다.

### vLLM (`integrations/vllm/single-dram-tier.patch`)
| 파일 | 내용 |
| --- | --- |
| `cpu/manager.py` | eviction 확인 단계(`set_eviction_gate`), 거부된 블록 보관, 되살리기, `restore()`. 이 단계를 켜지 않으면 기존 동작과 같음 |
| `cpu/shared_offload_region.py` | `SharedOffloadRegion.borrow()`: `/dev/shm` 파일 없이 Guard pool 메모리 위에 영역을 잡음 |
| `tiering/kvcr/manager.py` | `single_dram_tier` 모드: pool을 claim하고 그 메모리를 primary view로 사용, cascade/promotion 때 commit, retire 처리, LOCAL_G2 이벤트 생략 |
| `tiering/kvcr/shared_tier.py` | worker가 scheduler보다 먼저 뜨므로, 먼저 실행되는 쪽이 claim하고 같은 프로세스 안에서 공유 |
| `tiering/spec.py` | 위 조각들을 연결, TP=1 검증 |
| `tiering/factory.py` | 타입만 수정 (view를 직접 만드는 tier는 `None`을 받음) |

## 4. 설정

`agg-memory-service.yaml`을 기준으로 바꿀 부분만 적습니다.

```jsonc
// main 컨테이너 kv_transfer_config
"kv_connector_extra_config": {
  "cpu_bytes_to_use": 2147483648,          // ≤ Guard pool 크기
  "spec_name": "TieringOffloadingSpec",
  "secondary_tiers": [{
    "type": "kvcr",
    "single_dram_tier": true,              // 추가
    "kvcr_service_socket_path": "/run/kvcr/memory.sock",
    "compatibility_digest": "...",         // cpu_bytes_to_use나 KV 레이아웃이 바뀌면 같이 바꿀 것
    ...
  }]
}
```

- **`--pool-sizes-gb`:** `cpu_bytes_to_use` 이상이어야 합니다. 2 GiB(`2147483648`)면 `--pool-sizes-gb 2`로 충분합니다. 작으면 시작할 때 `raise --pool-sizes-gb` 에러가 납니다.
- **`--guard-count`:** pod 안의 engine(DP) 수입니다. GPU 수가 아닙니다.
- **`kvcr-memory` 볼륨:** `sizeLimit`은 pool + 100 MiB journal + handback 여유 이상이어야 합니다. `kvcr-services`의 memory limit도 그만큼 필요합니다. pool 페이지를 `kvcr-services`가 미리 할당하기 때문입니다.
- **`/dev/shm`:** 이 모드에서는 vLLM의 `/dev/shm/vllm_offload_*`를 만들지 않습니다. operator 기본값 8Gi면 충분합니다.

## 5. 제약

- **TP=PP=1만 지원합니다.** worker와 scheduler가 같은 프로세스여야 하기 때문에, multiproc executor면 두 번째 claim이 거부됩니다. TP≥2는 아래 7번 항목을 보세요.
- **SSD spill 중에는 store가 건너뛰어질 수 있습니다.** eviction 대상이 SSD로 쓰이는 동안 그 slot을 쓸 수 없어서, 빈 slot이 모자라면 `prepare_store`가 None을 돌려주고 그 store를 건너뜁니다. 예전 KVCR은 이럴 때 기다렸습니다.
- **`reset_cache`가 오래 걸릴 수 있습니다.** 모든 slot을 회수할 때까지 기다리고, 기본 정책에서는 전부 SSD로 spill한 다음에 끝납니다.

## 6. 로컬 검증 결과 (macOS, B200 아님)

| 항목 | 결과 |
| --- | --- |
| kvcr `tests/unit` | 509 통과. 실패 45개는 main과 똑같고(pidfd, libatomic 등 Linux 전용), 새로 생긴 실패는 없음 |
| vLLM `tests/v1/kv_offload` + offloading connector | 1079 통과 (base보다 +32). 새로 생긴 실패 없음. 나머지 실패는 Mac에 `/dev/shm`과 GPU가 없어서 |
| 새 테스트 | kvcr 26개 (`test_kvcr_framework_slots.py`, `test_g3.py`), vLLM 32개 (`test_manager_eviction_gate.py`, `test_kvcr_single_dram_tier.py`) |
| 결함 주입 검사 | kvcr 3개, vLLM 4개를 일부러 넣었고 모두 테스트에 걸림 |
| lint | 두 레포 모두 ruff 통과. vLLM mypy의 남은 1건은 base에도 있던 것 |
| 패치 적용 | 고정 커밋을 새로 checkout해서 `git apply --check` 통과 |
| Docker 이미지 빌드 | **하지 못함** (로컬에 docker 없음) |

## 7. B200 검증 계획

**새 이미지가 필요합니다.**
- kvcr 코드와 vLLM 패치가 모두 이미지 안에 들어가야 합니다.
- `Dockerfile.quick-start`가 패치를 적용하고, 바뀐 vLLM 파일 10개를 base 이미지에 덮어씁니다.
- base 이미지의 vLLM nightly(`43b4aaea`)와 고정 커밋을 비교해 봤습니다. 패치가 건드리는 파일은 어댑터(`kvcr/manager.py`, 원래 덮어쓰던 파일)를 빼면 내용이 같습니다. 그래서 덮어써도 버전이 섞이지 않습니다.

### 7.1 이미지 빌드
```bash
git clone https://github.com/changhyeonnam/kvcr.git && cd kvcr
git checkout feat/framework-managed-pool
DOCKER_BUILDKIT=1 docker build \
  --file Dockerfile.quick-start \
  --tag <registry>/kvcr-quick-start:single-dram \
  .
docker push <registry>/kvcr-quick-start:single-dram
```
빌드 마지막의 호환성 검사에 `single_dram_tier`와 `set_eviction_gate` 확인을 추가해 두었습니다. 패치가 빠지면 빌드가 실패합니다.

### 7.2 kvcr 단위 테스트 (Linux에서 전체 suite)
Mac에서 돌지 않던 Guard, pidfd, libatomic 테스트가 여기서 처음 돕니다.
```bash
cd kvcr
uv sync
uv run pytest tests/unit -q
uv run pytest tests/unit/test_kvcr_framework_slots.py tests/unit/test_g3.py \
  tests/unit/test_guard.py tests/unit/test_guard_integration.py \
  tests/unit/test_kvcr_service.py tests/unit/test_kvcr_service_workflow.py -q
```
기대 결과는 전부 통과입니다.

### 7.3 vLLM 단위 테스트 (빌드한 이미지 안, GPU)
```bash
git clone https://github.com/vllm-project/vllm.git vllm-pin && cd vllm-pin
git checkout 08567505b09324f891797f4a13c1bd73fc75a827
git apply ../kvcr/integrations/vllm/single-dram-tier.patch
docker run --rm --gpus all --shm-size 8g -v "$PWD/tests:/opt/vllm-src/tests" \
  -w /opt/vllm-src <registry>/kvcr-quick-start:single-dram \
  python3 -m pytest -q \
    tests/v1/kv_offload/cpu/test_manager.py \
    tests/v1/kv_offload/cpu/test_manager_eviction_gate.py \
    tests/v1/kv_offload/cpu/test_shared_offload_region.py \
    tests/v1/kv_offload/tiering/test_kvcr_tier.py \
    tests/v1/kv_offload/tiering/test_kvcr_single_dram_tier.py \
    tests/v1/kv_offload/tiering/test_tiering_offloading.py
```
- `tests` 디렉터리만 마운트하므로, `vllm` 모듈은 이미지에 설치된 패치 버전을 씁니다.
- 이미지에 pytest가 없으면 먼저 `pip install pytest`를 하세요.

### 7.4 e2e (k8s, `agg-memory-service.yaml`)
1. `DYNAMO_VLLM_IMAGE`를 새 이미지로 바꾸고, 4번의 `"single_dram_tier": true`를 추가해서 배포합니다.
2. 기동 확인:
   - `main` 로그: `Primary tier lives in the KVCR service pool at 0x...; 0 chunk(s) recovered`
   - `main` 로그: `cudaHostRegister rank=0 ... GB`. Guard pool이 GPU용으로 pin됐다는 뜻이고, **아직 확인하지 못한 부분**입니다.
   - `kubectl exec <pod> -c main -- ls /dev/shm`에 `vllm_offload_*`가 없어야 합니다.
3. 정확성:
   - 같은 긴 prefix prompt를 두 번 보내고, 출력이 같은지 확인합니다 (temperature 0).
   - offload hit 메트릭과 TTFT를 기존 이미지와 비교합니다.
4. 압박 테스트:
   - pool보다 큰 working set을 보내서 eviction과 SSD spill이 일어나게 합니다. G3를 설정한 경우입니다.
   - 확인할 것: 출력 정확성, `prepare_store` 실패나 store skip 빈도, SSD write량
5. 크래시 복구:
   - `kubectl exec <pod> -c main -- pkill -9 -f EngineCore`
     - pool을 claim한 프로세스는 `VLLM::EngineCore`입니다. 컨테이너의 PID 1(`dynamo.vllm`)은 같은 namespace 안에서 보낸 SIGKILL을 무시하기 때문에 대상으로 삼지 않습니다.
   - `kvcr-services` 로그에 `KVCR_EVENT guard_promoted ... recovered_blocks=N`이 나와야 합니다.
   - 그동안 다른 worker에서 peer hit이 유지되는지 봅니다 (Guard가 서빙).
   - `main`이 재시작되면 `N chunk(s) recovered`가 나오고, 같은 prompt가 바로 hit해야 합니다.
6. 메모리:
   - `kvcr-memory` 사용량이 pool + journal 정도인지 봅니다.
   - `main`의 RSS에 2 GiB CPU tier가 별도로 잡히지 않는지 봅니다. 예전에는 DRAM에 두 벌 있었습니다.

### 7.5 실패하면 먼저 볼 곳
| 증상 | 확인할 것 |
| --- | --- |
| 기동 시 `pool-sizes-gb` 에러 | `--pool-sizes-gb`가 `cpu_bytes_to_use`보다 작음 |
| 기동 시 `compatibility_digest` 에러 | 이전 pool이 다른 geometry로 남아 있음. digest를 바꾸거나 서비스를 재시작 |
| `cudaHostRegister failed` | pool이 pageable로 동작함 (성능 저하, 정확성은 유지). tmpfs와 IPC_LOCK, memlock 설정 확인 |
| 출력이 틀림 | 가장 먼저 의심할 곳은 retire 순서와 rescue. `KVCR_KEY_HISTORY=1` + `KVCR_LOG_LEVEL=DEBUG`로 key 이력을 확인 |
| 두 번째 claim 거부 | executor가 multiproc임 (TP=1인데 mp). `distributed_executor_backend` 확인 |

## 8. 다음 단계: TP≥2

pool은 지금처럼 engine당 하나를 쓰고, 모든 TP worker가 같이 매핑해서 각자 자기 rank 영역만 씁니다. 그래서 pool을 더 만들 필요는 없고, 다음이 필요합니다.

1. **kvcr 서비스 프로토콜:** worker co-attach와 여러 pidfd fencing. 등록된 프로세스가 전부 종료된 뒤에만 promote하고 handback합니다.
2. **vLLM:** worker가 scheduler의 claim을 기다리지 않고 서비스에 직접 attach하는 경로. 엔진 초기화 순서상 기다리면 deadlock이 됩니다.
3. **재시작 시 rank와 GPU의 대응 유지:** rank는 `current_device_index % world_size`로 계산하기 때문에 대응이 바뀌면 안 됩니다. `compatibility_digest`에 TP 크기를 포함합니다.

TP=1을 B200에서 검증한 뒤에 진행합니다.
