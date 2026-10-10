import os
import random
import uuid
import requests
from dotenv import load_dotenv

# .env 파일에서 실제 서버와 동일한 환경 변수 로드
load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000/api/v1")
ADMIN_KEY = os.getenv("ADMIN_KEY")

if not ADMIN_KEY:
    raise RuntimeError(".env 파일에 ADMIN_KEY가 설정되어 있지 않습니다.")

def test_fixes():
    print("=== 개선 사항 검증 시작 ===")
    headers_admin = {"X-Admin-Key": ADMIN_KEY}
    
    # 반복 실행 시 DB 충돌이 없도록 매번 임의의 8자리 학번 생성
    std_id = f"2026{random.randint(1000, 9999)}"

    # 1. 1번 판 시작 및 기획서 점수 계산 검증
    res_start = requests.post(f"{BASE_URL}/plays", json={
        "student_id": std_id, "name": "테스트", "department": "컴공",
        "client_request_id": str(uuid.uuid4())
    })
    assert res_start.status_code == 201, f"시작 실패: {res_start.text}"
    play1_id = res_start.json()["play_id"]

    from server import calculate_score_and_combo
    ore_counts = {"A": 0, "B": 2, "C": 9, "D": 14, "E": 21}
    score, combo, mult = calculate_score_and_combo(ore_counts)
    assert score == 70850, f"점수 불일치: {score}"
    assert combo == "B+C+D+E"
    assert mult == 2.6
    print("[PASS] 1. 기획서 가치표/조합표 기준 70,850점 및 B+C+D+E 콤보 계산 일치 확인")

    # 2. 2번, 3번 판 정상 발급 확인
    res_start2 = requests.post(f"{BASE_URL}/plays", json={
        "student_id": std_id, "name": "테스트", "department": "컴공",
        "client_request_id": str(uuid.uuid4())
    })
    res_start3 = requests.post(f"{BASE_URL}/plays", json={
        "student_id": std_id, "name": "테스트", "department": "컴공",
        "client_request_id": str(uuid.uuid4())
    })
    assert res_start2.status_code == 201 and res_start2.json()["play_no"] == 2
    assert res_start3.status_code == 201 and res_start3.json()["play_no"] == 3
    print("[PASS] 2. 1, 2, 3번 판 정상 발급 확인")

    # 3. 1번 판 환불 삭제 후 빈 슬롯(1번) 재채번 검증
    res_void = requests.post(
        f"{BASE_URL}/admin/plays/{play1_id}/void",
        headers=headers_admin,
        json={"refund": True}
    )
    # 관리자 키 인증 성공 여부 확인 (403 방지)
    assert res_void.status_code == 200, f"환불 요청 실패 (상태코드: {res_void.status_code}, 내용: {res_void.text})"
    
    # check 확인 (남은 횟수 1)
    res_check = requests.post(f"{BASE_URL}/players/check", json={
        "student_id": std_id, "name": "테스트", "department": "컴공"
    })
    assert res_check.status_code == 200
    assert res_check.json()["plays_remaining"] == 1, f"남은 횟수 불일치: {res_check.json()}"
    
    # 빈 슬롯인 1번 번호가 다시 발급되는지 확인
    res_start_refunded = requests.post(f"{BASE_URL}/plays", json={
        "student_id": std_id, "name": "테스트", "department": "컴공",
        "client_request_id": str(uuid.uuid4())
    })
    assert res_start_refunded.status_code == 201
    assert res_start_refunded.json()["play_no"] == 1
    print("[PASS] 3. 환불 후 비어 있는 1번 슬롯 재채번 및 정상 시작 성공 확인")

    # 4. 환불 없는 무효화(refund=False) 시 횟수 차감 유지 확인
    play2_id = res_start2.json()["play_id"]
    res_void2 = requests.post(
        f"{BASE_URL}/admin/plays/{play2_id}/void",
        headers=headers_admin,
        json={"refund": False}
    )
    assert res_void2.status_code == 200

    # 총 3판(1번 재발급, 2번 voided, 3번 유지)이 DB에 존재하므로 잔여 횟수는 0
    res_check_void = requests.post(f"{BASE_URL}/players/check", json={
        "student_id": std_id, "name": "테스트", "department": "컴공"
    })
    assert res_check_void.json()["plays_remaining"] == 0
    
    # 4번째 판 시도 시 차단 확인
    res_blocked = requests.post(f"{BASE_URL}/plays", json={
        "student_id": std_id, "name": "테스트", "department": "컴공",
        "client_request_id": str(uuid.uuid4())
    })
    assert res_blocked.status_code == 403
    print("[PASS] 4. 환불 없는 void 처리 시 횟수 차감 유지 및 추가 생성 차단 확인")

    print("\n=== 모든 개선 사항 완벽 통과 ===")

if __name__ == "__main__":
    test_fixes()