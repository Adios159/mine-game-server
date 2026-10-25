import json
import logging
import os
import re
import secrets
import unicodedata
import uuid
from dotenv import load_dotenv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    create_engine,
    desc,
    func,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ranking_server")
load_dotenv()

# ==========================================
# 1. 환경 변수 및 DB 연결 설정 (Neon/Postgres 호환)
# ==========================================
ADMIN_KEY = os.getenv("ADMIN_KEY")
if not ADMIN_KEY:
    raise RuntimeError(
        "CRITICAL: 'ADMIN_KEY' 환경 변수가 설정되지 않았습니다. 보안을 위해 서버 시작을 중단합니다."
    )

# 1. 환경 변수 문자열 앞뒤 공백 및 따옴표 정제
raw_db_url = os.getenv("DATABASE_URL", "").strip().strip("'\"")

if not raw_db_url:
    DATABASE_URL = "sqlite:///./ranking.db"
else:
    # 2. psycopg(v3) 명시 바인딩: postgresql+psycopg:// 로 통일
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif raw_db_url.startswith("postgresql://"):
        raw_db_url = raw_db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    # 3. Neon 연결 문자열에 포함될 수 있는 channel_binding 파라미터 안전하게 제거
    if "&channel_binding=require" in raw_db_url:
        raw_db_url = raw_db_url.replace("&channel_binding=require", "")
    elif "?channel_binding=require&" in raw_db_url:
        raw_db_url = raw_db_url.replace("channel_binding=require&", "")
    elif "?channel_binding=require" in raw_db_url:
        raw_db_url = raw_db_url.replace("?channel_binding=require", "")

    DATABASE_URL = raw_db_url

engine_kwargs: Dict[str, Any] = {"pool_pre_ping": True}
if "sqlite" in DATABASE_URL:
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

MAX_ORE_TOTAL = int(os.getenv("MAX_ORE_TOTAL", "220"))
THEORETICAL_MAX_SCORE = 3_630_000  # 220개 * 5,000점 * 3.3배율 (이론상 상한)
MIN_PLAY_SECONDS = 115
VALIDATE_SCORE_LOGIC = os.getenv("VALIDATE_SCORE_LOGIC", "true").lower() == "true"
ALLOWED_ORE_TYPES: Set[str] = {"A", "B", "C", "D", "E"}
# ==========================================
# 2. 게임 밸런스 데이터 로드 (S-04 공유 테이블)
# ==========================================
GAME_DATA_PATH = Path(os.getenv("GAME_DATA_PATH", "./game_data.json"))

DEFAULT_GAME_DATA = {
    "ore_values": {"A": 5000, "B": 3000, "C": 1000, "D": 500, "E": 250},
    "combos": [
        {"name": "D+E", "ores": ["D", "E"], "multiplier": 1.4},
        {"name": "C+D", "ores": ["C", "D"], "multiplier": 1.6},
        {"name": "B+C+D+E", "ores": ["B", "C", "D", "E"], "multiplier": 2.6},
        {"name": "A+C+D+E", "ores": ["A", "C", "D", "E"], "multiplier": 3.0},
        {"name": "A+B+C+E", "ores": ["A", "B", "C", "E"], "multiplier": 3.3},
    ],
}

if not GAME_DATA_PATH.exists():
    with open(GAME_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_GAME_DATA, f, ensure_ascii=False, indent=2)
    game_data = DEFAULT_GAME_DATA
else:
    with open(GAME_DATA_PATH, "r", encoding="utf-8") as f:
        game_data = json.load(f)

ORE_VALUES: Dict[str, int] = game_data["ore_values"]
COMBOS: List[Dict[str, Any]] = game_data["combos"]

# ==========================================
# 3. 데이터 모델 (DateTime timezone=True 적용)
# ==========================================
class Base(DeclarativeBase):
    pass

class Player(Base):
    __tablename__ = "players"

    student_id = Column(String(8), primary_key=True)
    name = Column(String(30), nullable=False)
    department = Column(String(50), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

class Play(Base):
    __tablename__ = "plays"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    student_id = Column(String(8), ForeignKey("players.student_id"), nullable=False)
    play_no = Column(Integer, nullable=False)
    status = Column(String(10), nullable=False, default="started")  # started, finished, voided
    request_id = Column(String(36), nullable=False)
    token = Column(String(64), nullable=False)
    started_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)
    score = Column(Integer, nullable=True)
    ore_counts = Column(JSON, nullable=True)
    combo = Column(String(50), nullable=True)
    client_version = Column(String(20), nullable=True)

    __table_args__ = (
        UniqueConstraint("student_id", "play_no", name="uq_plays_student_play_no"),
        UniqueConstraint("student_id", "request_id", name="uq_plays_student_request_id"),
    )

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 4. 유틸리티 함수
# ==========================================
def normalize_text(text: str, remove_internal_spaces: bool = False) -> str:
    norm = unicodedata.normalize("NFC", text.strip())
    if remove_internal_spaces:
        return re.sub(r"\s+", "", norm)
    return norm

def make_error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )

def mask_student_id(std_id: str) -> str:
    if len(std_id) == 8:
        return f"{std_id[:2]}****{std_id[-2:]}"
    return std_id

def format_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def calculate_score_and_combo(ore_counts: Dict[str, int]) -> tuple[int, str, float]:
    base_sum = sum(ore_counts.get(ore, 0) * ORE_VALUES.get(ore, 0) for ore in ORE_VALUES)

    achieved_combos = []
    for combo in COMBOS:
        if all(ore_counts.get(ore, 0) >= 1 for ore in combo["ores"]):
            achieved_combos.append(combo)

    if achieved_combos:
        best_combo = max(achieved_combos, key=lambda c: c["multiplier"])
        multiplier = best_combo["multiplier"]
        combo_name = best_combo["name"]
    else:
        multiplier = 1.0
        combo_name = "NONE"

    calculated_score = int(round(base_sum * multiplier))
    return calculated_score, combo_name, multiplier

def compute_rankings(db: Session) -> List[Dict[str, Any]]:
    # 순위 결정성 보장: 최고점 DESC -> 달성 시각 ASC -> student_id ASC (동점 완전 해소)
    subquery = (
        select(
            Play.student_id,
            Play.score,
            Play.finished_at,
            func.row_number().over(
                partition_by=Play.student_id,
                order_by=[desc(Play.score), Play.finished_at.asc()]
            ).label("rn")
        )
        .where(Play.status == "finished", Play.score.isnot(None))
        .subquery()
    )

    query = (
        select(
            Player.student_id,
            Player.name,
            Player.department,
            subquery.c.score.label("best_score"),
            subquery.c.finished_at.label("achieved_at"),
        )
        .join(subquery, Player.student_id == subquery.c.student_id)
        .where(subquery.c.rn == 1)
        .order_by(desc(subquery.c.score), subquery.c.finished_at.asc(), Player.student_id.asc())
    )

    rows = db.execute(query).fetchall()
    rank_list = []
    for idx, row in enumerate(rows, start=1):
        rank_list.append({
            "rank": idx,
            "student_id": row.student_id,
            "student_id_masked": mask_student_id(row.student_id),
            "name": row.name,
            "department": row.department,
            "best_score": row.best_score,
            "achieved_at": format_iso_utc(row.achieved_at),
        })
    return rank_list

# ==========================================
# 5. Pydantic 요청 스키마
# ==========================================
STUDENT_ID_REGEX = r"^[0-9]{8}$"

class PlayerCheckReq(BaseModel):
    student_id: str = Field(..., pattern=STUDENT_ID_REGEX)
    name: str = Field(..., min_length=1, max_length=30)
    department: str = Field(..., min_length=1, max_length=50)

class PlayStartReq(BaseModel):
    student_id: str = Field(..., pattern=STUDENT_ID_REGEX)
    name: str = Field(..., min_length=1, max_length=30)
    department: str = Field(..., min_length=1, max_length=50)
    client_request_id: str = Field(..., min_length=1, max_length=36)
    client_version: Optional[str] = Field("0.1.0", max_length=20)

class PlayFinishReq(BaseModel):
    score: int = Field(..., ge=0, le=THEORETICAL_MAX_SCORE)
    ore_counts: Dict[str, int]
    combo: Optional[str] = Field(None, max_length=50)
    client_version: Optional[str] = Field("0.1.0", max_length=20)

class AdminPlayerPatchReq(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=30)
    department: Optional[str] = Field(None, min_length=1, max_length=50)

class AdminVoidReq(BaseModel):
    refund: bool = False

# ==========================================
# 6. FastAPI 앱 및 전역 에러 핸들러 통일
# ==========================================
app = FastAPI(title="Mining Game Ranking API", version="1.2.0")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return make_error_response(422, "INVALID_INPUT", "필수 항목이 비었거나 형식 또는 길이가 맞지 않습니다.")

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    code = "HTTP_ERROR"
    if exc.status_code == 404:
        code = "NOT_FOUND"
    elif exc.status_code == 405:
        code = "METHOD_NOT_ALLOWED"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail)}},
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled server error: {exc}")
    return make_error_response(500, "INTERNAL_SERVER_ERROR", "서버 내부 오류가 발생했습니다.")

# ==========================================
# 7. 엔드포인트 구현
# ==========================================

# 7.1 GET /health
@app.get("/api/v1/health")
def health():
    return {
        "status": "ok",
        "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

# 7.2 POST /players/check
@app.post("/api/v1/players/check")
def check_player(req: PlayerCheckReq, db: Session = Depends(get_db)):
    std_id = normalize_text(req.student_id)
    name = normalize_text(req.name)
    dept = normalize_text(req.department)

    player = db.get(Player, std_id)
    if not player:
        return {
            "registered": False,
            "plays_used": 0,
            "plays_remaining": 3,
            "best_score": None,
            "rank": None,
        }

    if (normalize_text(player.name, True) != normalize_text(name, True) or
            normalize_text(player.department, True) != normalize_text(dept, True)):
        return make_error_response(409, "PLAYER_INFO_MISMATCH", "이미 등록된 학번이며 이름 또는 학과가 다릅니다.")

    plays_used = db.query(Play).filter(Play.student_id == std_id).count()
    plays_remaining = max(0, 3 - plays_used)

    all_ranks = compute_rankings(db)
    my_rank_info = next((item for item in all_ranks if item["student_id"] == std_id), None)

    return {
        "registered": True,
        "plays_used": plays_used,
        "plays_remaining": plays_remaining,
        "best_score": my_rank_info["best_score"] if my_rank_info else None,
        "rank": my_rank_info["rank"] if my_rank_info else None,
    }

# 7.3 POST /plays (재시도 충돌 및 콜드스타트 오판 해결)
@app.post("/api/v1/plays", status_code=status.HTTP_201_CREATED)
def start_play(req: PlayStartReq, db: Session = Depends(get_db)):
    std_id = normalize_text(req.student_id)
    name = normalize_text(req.name)
    dept = normalize_text(req.department)

    player = db.get(Player, std_id)
    if player:
        if (normalize_text(player.name, True) != normalize_text(name, True) or
                normalize_text(player.department, True) != normalize_text(dept, True)):
            return make_error_response(409, "PLAYER_INFO_MISMATCH", "등록된 학번 정보와 일치하지 않습니다.")
    else:
        player = Player(student_id=std_id, name=name, department=dept)
        db.add(player)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            player = db.get(Player, std_id)

    # 1차 멱등성 검사: request_id 확인
    existing_req_play = db.query(Play).filter(
        Play.student_id == std_id,
        Play.request_id == req.client_request_id
    ).first()

    if existing_req_play:
        plays_used = db.query(Play).filter(Play.student_id == std_id).count()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "play_id": existing_req_play.id,
                "play_token": existing_req_play.token,
                "play_no": existing_req_play.play_no,
                "plays_used": plays_used,
                "plays_remaining": max(0, 3 - plays_used),
                "started_at": format_iso_utc(existing_req_play.started_at),
            }
        )

    # 빈 슬롯 채번
    existing_plays = db.query(Play.play_no).filter(Play.student_id == std_id).all()
    used_play_nos: Set[int] = {p[0] for p in existing_plays}

    if len(used_play_nos) >= 3:
        return make_error_response(403, "PLAY_LIMIT_REACHED", "플레이 횟수를 모두 사용했습니다.")

    available_nos = [no for no in [1, 2, 3] if no not in used_play_nos]
    if not available_nos:
        return make_error_response(403, "PLAY_LIMIT_REACHED", "플레이 횟수를 모두 사용했습니다.")

    next_play_no = min(available_nos)
    token = secrets.token_urlsafe(32)

    new_play = Play(
        student_id=std_id,
        play_no=next_play_no,
        request_id=req.client_request_id,
        token=token,
        client_version=req.client_version,
    )
    db.add(new_play)

    try:
        db.commit()
        db.refresh(new_play)
    except IntegrityError:
        db.rollback()
        # 동시 중복 요청 오판 방지: 재시도로 인한 request_id 중복인지 우선 조회
        duplicated_play = db.query(Play).filter(
            Play.student_id == std_id,
            Play.request_id == req.client_request_id
        ).first()

        if duplicated_play:
            plays_used = db.query(Play).filter(Play.student_id == std_id).count()
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "play_id": duplicated_play.id,
                    "play_token": duplicated_play.token,
                    "play_no": duplicated_play.play_no,
                    "plays_used": plays_used,
                    "plays_remaining": max(0, 3 - plays_used),
                    "started_at": format_iso_utc(duplicated_play.started_at),
                }
            )
        # request_id 중복이 아니면 실제 4번째 판 생성 충돌이므로 403 반환
        return make_error_response(403, "PLAY_LIMIT_REACHED", "플레이 횟수를 모두 사용했습니다.")

    total_used = len(used_play_nos) + 1
    return {
        "play_id": new_play.id,
        "play_token": new_play.token,
        "play_no": new_play.play_no,
        "plays_used": total_used,
        "plays_remaining": 3 - total_used,
        "started_at": format_iso_utc(new_play.started_at),
    }

# 7.4 POST /plays/{play_id}/finish (원자적 업데이트 및 최소 유효성 검사)
@app.post("/api/v1/plays/{play_id}/finish")
def finish_play(
    play_id: str,
    req: PlayFinishReq,
    x_play_token: Optional[str] = Header(None, alias="X-Play-Token"),
    db: Session = Depends(get_db)
):
    play = db.get(Play, play_id)
    if not play or play.status == "voided":
        return make_error_response(404, "PLAY_NOT_FOUND", "해당 플레이를 찾을 수 없습니다.")

    if not x_play_token or play.token != x_play_token:
        return make_error_response(403, "INVALID_TOKEN", "유효하지 않은 플레이 토큰입니다.")

    now = datetime.now(timezone.utc)

    # 1. 이미 finished 상태인 경우 멱등성 체크
    if play.status == "finished":
        if play.score == req.score and play.ore_counts == req.ore_counts:
            all_ranks = compute_rankings(db)
            my_rank_info = next((r for r in all_ranks if r["student_id"] == play.student_id), None)
            top_10 = [{
                "rank": r["rank"],
                "name": r["name"],
                "department": r["department"],
                "best_score": r["best_score"]
            } for r in all_ranks[:10]]
            plays_used = db.query(Play).filter(Play.student_id == play.student_id).count()

            return {
                "play_id": play.id,
                "score": play.score,
                "is_personal_best": my_rank_info is not None and my_rank_info["best_score"] == play.score,
                "best_score": my_rank_info["best_score"] if my_rank_info else play.score,
                "rank": my_rank_info["rank"] if my_rank_info else None,
                "total_players": len(all_ranks),
                "plays_remaining": max(0, 3 - plays_used),
                "top": top_10,
            }
        else:
            return make_error_response(409, "PLAY_ALREADY_FINISHED", "이미 다른 점수로 종료된 판입니다.")

    # 2. 광석 키 및 개수 제한 (검증 해제 시에도 필수 차단)
    if not set(req.ore_counts.keys()).issubset(ALLOWED_ORE_TYPES) or any(cnt < 0 for cnt in req.ore_counts.values()):
        return make_error_response(422, "SCORE_INVALID", "광석 종류 및 개수가 올바르지 않습니다.")

    total_ores = sum(req.ore_counts.values())
    if total_ores > MAX_ORE_TOTAL:
        return make_error_response(422, "SCORE_INVALID", f"광석 합계는 {MAX_ORE_TOTAL}개를 넘을 수 없습니다.")

    # 3. 소요 시간 검증 (115초, timezone-aware 비교)
    play_started_at = play.started_at
    if play_started_at.tzinfo is None:
        play_started_at = play_started_at.replace(tzinfo=timezone.utc)
    else:
        play_started_at = play_started_at.astimezone(timezone.utc)

    elapsed = (now - play_started_at).total_seconds()
    if elapsed < MIN_PLAY_SECONDS:
        return make_error_response(422, "TOO_EARLY", f"플레이 시간이 너무 짧습니다 ({elapsed:.1f}초 경과).")

    # 4. 점수 일치 계산
    expected_score, server_combo_name, mult = calculate_score_and_combo(req.ore_counts)

    if req.combo and req.combo != server_combo_name:
        logger.info(
            f"콤보 불일치: 클라이언트={req.combo}, 서버 계산={server_combo_name} (배율 x{mult})"
        )

    if VALIDATE_SCORE_LOGIC and req.score != expected_score:
        return make_error_response(
            422,
            "SCORE_INVALID",
            f"광석 조합 및 배율로 계산된 점수와 일치하지 않습니다 (기대값: {expected_score})."
        )

    # 5. 원자적 UPDATE (status='started' 조건 단일 쿼리로 동시 종료 방지)
    stmt = (
        update(Play)
        .where(Play.id == play_id, Play.status == "started")
        .values(
            status="finished",
            score=req.score,
            ore_counts=req.ore_counts,
            combo=server_combo_name,
            finished_at=now,
        )
    )
    result = db.execute(stmt)
    db.commit()

    if result.rowcount == 0:
        # 동시 요청이 먼저 커밋된 경우: 상태 재조회 후 멱등성 판정
        db.refresh(play)
        if play.status == "finished":
            if play.score == req.score and play.ore_counts == req.ore_counts:
                all_ranks = compute_rankings(db)
                my_rank_info = next((r for r in all_ranks if r["student_id"] == play.student_id), None)
                top_10 = [{
                    "rank": r["rank"],
                    "name": r["name"],
                    "department": r["department"],
                    "best_score": r["best_score"]
                } for r in all_ranks[:10]]
                plays_used = db.query(Play).filter(Play.student_id == play.student_id).count()

                return {
                    "play_id": play.id,
                    "score": play.score,
                    "is_personal_best": my_rank_info is not None and my_rank_info["best_score"] == play.score,
                    "best_score": my_rank_info["best_score"] if my_rank_info else play.score,
                    "rank": my_rank_info["rank"] if my_rank_info else None,
                    "total_players": len(all_ranks),
                    "plays_remaining": max(0, 3 - plays_used),
                    "top": top_10,
                }
            return make_error_response(409, "PLAY_ALREADY_FINISHED", "이미 다른 점수로 종료된 판입니다.")
        return make_error_response(404, "PLAY_NOT_FOUND", "해당 플레이를 찾을 수 없습니다.")

    # 정상 업데이트 완료 응답
    all_ranks = compute_rankings(db)
    my_rank_info = next((r for r in all_ranks if r["student_id"] == play.student_id), None)
    top_10 = [{
        "rank": r["rank"],
        "name": r["name"],
        "department": r["department"],
        "best_score": r["best_score"]
    } for r in all_ranks[:10]]
    plays_used = db.query(Play).filter(Play.student_id == play.student_id).count()

    return {
        "play_id": play.id,
        "score": req.score,
        "is_personal_best": my_rank_info is not None and my_rank_info["best_score"] == req.score,
        "best_score": my_rank_info["best_score"] if my_rank_info else req.score,
        "rank": my_rank_info["rank"] if my_rank_info else None,
        "total_players": len(all_ranks),
        "plays_remaining": max(0, 3 - plays_used),
        "top": top_10,
    }

# 7.5 GET /ranking
@app.get("/api/v1/ranking")
def get_ranking(
    student_id: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=500),
    db: Session = Depends(get_db),
):
    all_ranks = compute_rankings(db)
    top_10 = [{
        "rank": r["rank"],
        "name": r["name"],
        "department": r["department"],
        "student_id_masked": r["student_id_masked"],
        "best_score": r["best_score"],
        "achieved_at": r["achieved_at"],
    } for r in all_ranks[:10]]

    me_info = None
    if student_id:
        std_id_norm = normalize_text(student_id)
        matched = next((r for r in all_ranks if r["student_id"] == std_id_norm), None)
        if matched:
            me_info = {
                "rank": matched["rank"],
                "best_score": matched["best_score"],
            }

    return {
        "total_players": len(all_ranks),
        "top": top_10,
        "me": me_info,
    }

# ==========================================
# 8. 관리용 엔드포인트
# ==========================================
def verify_admin_key(x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")):
    if not x_admin_key or not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "INVALID_ADMIN_KEY", "message": "관리자 인증 실패"}
        )

@app.patch("/api/v1/admin/players/{student_id}", dependencies=[Depends(verify_admin_key)])
def admin_patch_player(student_id: str, req: AdminPlayerPatchReq, db: Session = Depends(get_db)):
    player = db.get(Player, normalize_text(student_id))
    if not player:
        return make_error_response(404, "PLAYER_NOT_FOUND", "해당 학생을 찾을 수 없습니다.")

    if req.name:
        player.name = normalize_text(req.name)
    if req.department:
        player.department = normalize_text(req.department)

    db.commit()
    return {
        "student_id": player.student_id,
        "name": player.name,
        "department": player.department,
    }

@app.get("/api/v1/admin/export", dependencies=[Depends(verify_admin_key)])
def admin_export(db: Session = Depends(get_db)):
    all_ranks = compute_rankings(db)
    export_list = []
    for r in all_ranks:
        plays_used = db.query(Play).filter(Play.student_id == r["student_id"]).count()
        export_list.append({
            "student_id": r["student_id"],
            "name": r["name"],
            "department": r["department"],
            "best_score": r["best_score"],
            "achieved_at": r["achieved_at"],
            "plays_used": plays_used,
        })
    return {"players": export_list}

@app.post("/api/v1/admin/plays/{play_id}/void", dependencies=[Depends(verify_admin_key)])
def admin_void_play(play_id: str, req: AdminVoidReq, db: Session = Depends(get_db)):
    play = db.get(Play, play_id)
    if not play:
        return make_error_response(404, "PLAY_NOT_FOUND", "해당 플레이를 찾을 수 없습니다.")

    if req.refund:
        db.delete(play)
    else:
        play.status = "voided"
        play.score = None

    db.commit()
    return {"message": "플레이 무효화 처리 완료", "refunded": req.refund}