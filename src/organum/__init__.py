"""organum — Organism Engineering CLI.

상태와 규율의 도구지, 에이전트가 아니다. 포맷 계약: docs/format-v0.md.
"""

# 버전의 단일 소스 = 패키지 메타데이터(pyproject) — 이원화가 0.0.1 유령 버전을 낳았다(critic 감사 ⑥)
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    __version__ = _pkg_version("organum")
except PackageNotFoundError:  # 미설치 소스 실행(개발 체크아웃 등)
    __version__ = "0+unknown"

# 이 도구가 쓰고 읽는 .organum/ 포맷 버전 (docs/format-v0.md §4, §9)
FORMAT_VERSION = 0
