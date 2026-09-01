# AION2 Kakao Skill Server

카카오 비즈니스 챗봇 스킬 서버입니다.

고정 서버: 지켈

지원 명령어:
- !윤이 → 캐릭터 직업 / 전투력 / 장착 마석 총합
- !아그로 → 지켈 아그로 필드보스 시간
- !필보 → 지켈 필드보스 전체
- !공지 → AION2 최신 공지
- !CM → AION2 최신 CM
- !업데이트 → 최신 업데이트
- !도움 → 명령어

카카오 채널:
http://pf.kakao.com/_xorUxaX

## Render 배포

1. GitHub에 새 저장소 생성
2. 이 폴더의 파일 5개를 업로드
3. Render.com 로그인
4. New > Web Service
5. GitHub 저장소 연결
6. Docker 방식으로 자동 인식
7. Deploy

배포 완료 후 예:
https://aion2-kakao-skill.onrender.com

카카오 챗봇 관리자센터 스킬 URL:
https://aion2-kakao-skill.onrender.com/kakao/skill

Test URL도 동일하게 넣어도 됩니다.

헤더는 비워도 됩니다.

## 카카오 블록 연결
아래 블록 모두 같은 스킬을 연결하면 됩니다.
- 통합 검색
- 최신 공지
- 최신 CM
- 전체 필보
- 업데이트

서버가 userRequest.utterance 전체를 읽어서 명령을 구분합니다.

## 참고
NotMeter와 AION2 사이트 화면 구조가 바뀌면 파서 수정이 필요할 수 있습니다.
