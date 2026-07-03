# RVC upstream 검증 메모

검증일: 2026-07-12

현재 명령 어댑터 검토 기준 commit:
`7ef19867780cf703841ebafb565a4e47d1ea86ff`

이 문서는 업로드 설계의 RVC 관련 가정을 공식 upstream 자료와 대조한 결과다. `main` branch는 움직일 수 있으므로 이 메모가 runtime pin을 대신하지 않는다. 실제 Worker release 전에는 검증된 commit SHA와 adapter profile을 고정해야 한다.

## 확인한 공식 자료

- [RVC WebUI 공식 저장소](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
- [공식 Training Wiki](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/wiki/Instructions-and-tips-for-RVC-training)
- [공식 FAQ](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/wiki/FAQ-%28Frequently-Asked-Questions%29)
- [공식 MIT LICENSE](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/LICENSE)
- [현재 `infer-web.py`](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/infer-web.py)

## 고정 commit inference source 검증

명령 adapter와 sample inference 검토는 움직이는 `main`이 아니라 다음 commit의 파일 byte를
기준으로 한다.

- Commit: [`7ef19867780cf703841ebafb565a4e47d1ea86ff`](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/commit/7ef19867780cf703841ebafb565a4e47d1ea86ff)
- `infer/modules/vc/modules.py`: `93002953b30caea1a5081fc2966f9c8243f53277ceaf0caeffe492bf79026fe6`
- `infer/modules/vc/pipeline.py`: `ab2318d595ba6a219b1d8d6f8d29ca4fbf3a7f6f11462b8db226f71b8b2b3d41`
- `infer/modules/vc/utils.py`: `c4738c746cc321925c94349246330baa4dc2e9d6f1c07ce6db26d903cbe95596`
- `infer/lib/audio.py`: `f265d61a950706580690d45742b44c511f57f60ebf43ba5fc35a74e17b447400`
- `configs/config.py`: `3926f74d4114aaaa434e710c486bd32322d0fc2944b9ece533d44db771eeb3fc`

검증 대상 [`modules.py`](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/7ef19867780cf703841ebafb565a4e47d1ea86ff/infer/modules/vc/modules.py)는
`VC.get_vc()`에서 `weight_root/<sid>`를 기본 `torch.load`로 역직렬화하고, 알 수 없는
`version/f0` 조합을 v1 F0 synthesizer로 fallback한다. `get_index_path_from_model()`도
`index_root` 전체를 탐색하므로 Orchestrator wrapper는 `get_vc()`를 호출하거나 외부 model
이름과 environment root를 넘기지 않는다. exact class map과 검증된 metadata/state dict를
attempt-private `VC`/`Pipeline`에 직접 주입한다.

`VC.vc_single()`은 검증된 단일 입력에 재사용할 수 있지만 broad exception을 traceback
문자열과 `(None, None)`으로 반환한다. wrapper는 성공 tuple, sample rate와 1차원 non-empty
`int16` 결과를 별도로 검사하고 traceback이나 local path를 Manager 오류로 보내지 않는다.
[`pipeline.py`](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/7ef19867780cf703841ebafb565a4e47d1ea86ff/infer/modules/vc/pipeline.py)는
inference F0를 `pm|harvest|crepe|rmvpe`로 분기하지만 FAISS index load 오류를 no-index로
조용히 fallback한다. 따라서 `index_rate>0`인 Orchestrator Job은 same-attempt `final.index`의
size/SHA-256, v1 256/v2 768 dimension과 non-empty vector를 선검증하고 실패를 숨기지 않는다.

upstream resample 조건은 `tgt_sr != resample_sr >= 16000`이다. `1..15999`는 오류가 아니라
resample 미적용으로 해석되어 설정 의미가 모호해지므로 Job/Preset 계약은 `0` 또는
`16000..192000`만 허용한다. [`utils.py`](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/7ef19867780cf703841ebafb565a4e47d1ea86ff/infer/modules/vc/utils.py)의
HuBERT 상대 경로 때문에 실행 cwd는 검증된 attempt-private projection이어야 한다.
`load_dotenv`, 전역 argv parsing, config file 쓰기와 `argparse type=bool`을 포함한
[`tools/infer_cli.py`](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/7ef19867780cf703841ebafb565a4e47d1ea86ff/tools/infer_cli.py)는 직접 실행하지 않는다.

## 직렬화 신뢰와 sample 활성화 gate

reviewed upstream `pyproject.toml`의 Torch 2.4.0 marker는 source archive 변경 감지
기준으로 유지하지만 runtime 후보로 설치하지 않는다. 2.4.0은
`torch.load(..., weights_only=True)`에서도 code execution이 가능했던
[CVE-2025-32434/GHSA-53q9-r3pm-6pq6](https://github.com/advisories/GHSA-53q9-r3pm-6pq6)의
영향 범위 `<2.6.0`에 포함된다. 별도 release 후보 lock은 PyTorch 공식
[이전 버전 표](https://pytorch.org/get-started/previous-versions/)의 조합인 Torch
`2.6.0+cu124`, Torchvision `0.21.0+cu124`, Torchaudio `2.6.0+cu124`와 CUDA runtime
12.4다. PyTorch도 [untrusted model을 untrusted code처럼
취급](https://github.com/pytorch/pytorch/security/policy)하고
[`torch.load`에 신뢰하지 않는 데이터를 전달하지 말 것](https://docs.pytorch.org/docs/stable/generated/torch.load.html)을
요구한다. [serialization 문서](https://docs.pytorch.org/docs/stable/notes/serialization.html)의
2.6 기본값에 암묵적으로 기대지 않고 각 호출의 trust mode를 명시한다.

sample inference가 읽을 수 있는 model/index는 같은 pinned attempt가 생성하고 stage
metadata에 size/SHA-256이 결박된 결과뿐이다. HuBERT/RMVPE와 torchcrepe model asset은
operator가 source/license/size/SHA-256을 고정한 runtime manifest 입력만 허용한다. 사용자나
외부 storage가 제공한 `.pth`, `.pt`, `.index`는 현재 Worker process 경계에서 읽지 않는다.

CREPE 경로는 `runtime/crepe/full.pth` 하나로 고정했고 strict asset manifest 및
build-generated private projection의 exact inventory에 포함한다. runner/driver는 같은
`O_NOFOLLOW` FD byte의 size/SHA-256을 전·중·후에 검증하고
`torchcrepe.Crepe("full")` state dict를 명시적 `weights_only=True`로 strict load한 뒤
`infer.model/capacity`에 pre-bind한다. 같은-attempt small model도 `weights_only=True`다.
HuBERT/RMVPE는 외부 upload가 아니라 manifest-verified operator byte에 한해 명시적
`weights_only=False`로 읽는다. attempt-private `TORCH_HOME`, `HF_HUB_OFFLINE=1`,
`TRANSFORMERS_OFFLINE=1`을 사용하며 전역 loader override나 package cache/network fallback으로
이 호출별 경계를 바꾸지 않는다.

lease-bound TestSet 전송, CREPE safe-loader와 Sample completion gate가 코드로 연결됐더라도
다음 중 하나를 실제 GPU/no-network matrix로 검증하기 전에는 sample stage를 활성화하지 않는다.

1. 현재 Torch `2.6.0+cu124` 후보의 실제 digest-pinned amd64 image에서 고정
   RVC/fairseq/RMVPE/torchcrepe 전체 호환성, 호출별 trust mode와 v1/v2 sample matrix를
   검증한다.
2. same-attempt 배포 model을 tensor-only
   [safetensors](https://pytorch.org/projects/safetensors/)와 strict canonical JSON metadata로
   변환하고, 남아 있는 HuBERT/RMVPE 등의 pickle asset은 manifest에 고정된 operator-trusted
   byte만 읽도록 한 GPU matrix를 검증한다.

이 gate 전에는 `AUTO_SAMPLE_JOBS_ENABLED=false`, 빈 inference F0 capability,
`fixed_test_set_inference_ready=false`, `RVC_GPU_SMOKE_VERIFIED=false`,
`PROFILE_STAGE_SET_VERIFIED=false`, `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 유지한다.
현재 subprocess process-group 종료는
timeout/cancel 경계이지 악성 pickle을 격리하는 security sandbox가 아니다.

## 대조 결과

- Training Wiki는 지정 폴더 바로 아래 audio만 읽고 하위 폴더는 자동으로 읽지 않는다고 설명한다. 중앙 ingestion의 recursive flatten 요구와 일치한다.
- Wiki의 전처리 산출물은 `0_gt_wavs`, `1_16k_wavs`, F0 사용 시 `2a_f0`, `2b-f0nsf`다.
- Wiki는 v1 기준 HuBERT feature를 `3_feature256`에 저장하며, 현재 `infer-web.py`는 v1을 `3_feature256`, v2를 `3_feature768`로 분기한다.
- 현재 UI source의 training F0 선택지는 `pm`, `harvest`, `dio`, `rmvpe`, `rmvpe_gpu`이고 inference는 `pm`, `harvest`, `crepe`, `rmvpe`다.
- 현재 source는 pretrained root를 v1 `assets/pretrained`, v2 `assets/pretrained_v2`로 분기하고 F0 사용 시 파일명에 `f0` prefix를 붙인다.
- 현재 source의 학습 명령은 `infer/modules/train/train.py`에 experiment, sample rate, F0, batch, GPU, epoch, save, pretrained와 version 인자를 전달한다.
- index 생성은 version에 따라 256/768 dimension을 사용하고 `total_fea.npy`, `trained_*.index`, `added_*.index`를 만든다. Worker는 `added_*.index`만 inference용 최종 후보로 취급한다.
- Wiki와 FAQ는 G/D 파일을 학습 재개용 checkpoint로 설명한다. 배포용 small model은 별도 weights 산출물 또는 checkpoint 처리 기능의 추출 결과여야 한다.
- README는 `hubert_base.pt`, pretrained asset과 v2용 `pretrained_v2`, RMVPE 사용 시 `rmvpe.pt`를 요구한다.

## 구현에 반영할 제한

- 현재 source에는 v2 `32k` 선택지도 있으나 업로드 요구사항의 초기 범위는 `40k`, `48k`이므로 첫 contract는 두 값만 허용한다. 확장은 별도 요구사항과 pretrained matrix 검증 후 진행한다.
- upstream WebUI 자체는 문자열 command와 `shell=True`를 사용하지만 Orchestrator Worker는 동일 방식을 복사하지 않는다. 검증된 인자 배열로 `shell=False` 실행한다.
- 공식 저장소 source가 MIT인 것과 모든 pretrained/HubERT/RMVPE weight를 재배포할 권리가 있는 것은 별개다. 설치 bundle에 asset을 포함하기 전 파일별 출처와 라이선스를 확인한다.
- `main`의 명령 형태는 참고용이다. 2026-07-11 기준 HEAD
  `7ef19867780cf703841ebafb565a4e47d1ea86ff`의 argv를 command snapshot 기준으로
  기록했지만, release는 allowlist에 있는 repository와 commit별 adapter profile만 실행한다.

## 아직 필요한 검증

- Torch `2.6.0+cu124` 후보의 reviewed amd64 base digest, 실제
  Python/CUDA/PyTorch/fairseq/RMVPE/torchcrepe GPU/no-network 호환 matrix와 필요한 patch
- v1/v2 40k/48k, F0/non-F0 pretrained 파일 checksum
- loader policy를 포함한 vulnerability/container/secret scan과 asset별 재배포 라이선스 검토
- 고정 sample test set의 사용·배포 라이선스
