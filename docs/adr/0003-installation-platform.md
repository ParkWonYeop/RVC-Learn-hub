# ADR-0003: 1차 설치 플랫폼과 배포 파일

- 상태: Accepted
- 날짜: 2026-07-11

## 맥락

최종 목표는 중앙 서버와 학습 서버 설치 파일이지만 원문은 OS, driver, CUDA/PyTorch와 weight 배포 범위를 고정하지 않았다.

## 결정

- 1차 공식 대상은 Ubuntu 22.04/24.04 x86_64다.
- Manager는 Docker Engine과 Compose plugin을 preflight하고 여러 service를 설치하는 독립 bundle로 제공한다.
- Worker는 NVIDIA driver와 Container Toolkit을 preflight하는 별도 bundle로 제공한다.
- NVIDIA kernel driver는 설치 파일에 포함하거나 자동 변경하지 않는다.
- RVC source revision과 runtime image는 고정하지만 pretrained/HuBERT/RMVPE asset은 재배포 권한 확인 전 bundle에 포함하지 않는다.
- 각 release는 version manifest, image/파일 SHA-256, 설치/업그레이드/제거 절차를 포함한다. 제거는 기본적으로 데이터와 설정을 보존한다.

## 결과

GPU driver 호환 위험과 라이선스 위험을 설치 패키지 밖에서 명시적으로 관리할 수 있다. Windows와 다른 Linux 배포판은 v1.0 범위 밖이며 추후 별도 ADR이 필요하다.

