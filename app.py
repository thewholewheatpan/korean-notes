import streamlit as st
import datetime
import re
import pymupdf
from PIL import Image, ImageDraw
import io
import base64
import uuid
from supabase import create_client, Client

# --- 1. Supabase 연동 설정 ---
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "https://whojdlldrwcblhoyynsl.supabase.co").replace("/rest/v1/", "").rstrip("/")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indob2pkbGxkcndjYmxob3l5bnNsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTE1NTA1NDEsImV4cCI6MjEwNzEyNjU0MX0.MTQmLUEdQ40Go6ewY4gZgNy1woMEibEuocA4yKx3p-w")

@st.cache_resource
def init_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

supabase = init_supabase()

# DB 조회 결과 캐시: 체크박스를 누를 때마다 DB를 다시 부르지 않도록 함.
# 쓰기 작업 후에는 반드시 clear_db_cache()로 비워서 최신 내용이 보이게 함.
DB_CACHE_TTL = 600

def clear_db_cache():
    st.cache_data.clear()

# 서버(Streamlit Cloud)는 UTC 기준이라 한국 시간으로 고정
KST = datetime.timezone(datetime.timedelta(hours=9))

def now_kst_str():
    return datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M")

# 자연스러운 파일명 정렬 함수: 문장(2)가 문장(10)보다 앞에 오도록 숫자는 숫자 크기대로 비교
def natural_name_key(name):
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r'(\d+)', name)]

def natural_sort_key(file):
    return natural_name_key(file.name)

# --- 2. PDF 문제 자동 자르기 ---
# 문항 하나 = 영역(region) 여러 개. 영역은 (쪽 번호, x0, y0, x1, y1) PDF 좌표(pt).
# 다음 단·다음 쪽으로 이어지는 문제는 영역이 2개 이상이 되고, 이미지로 만들 때 위아래로 이어 붙임.
PDF_SAVE_DPI = 300
PDF_PREVIEW_DPI = 110
TOP_MARGIN = 10 * 72 / 300  # 300dpi 기준 10px

def _is_passage_start(text):
    return bool(
        text.startswith('※') or
        re.match(r'^[※\*]\s*<보기>', text) or
        re.match(r'^[※\*]\s*다음', text) or
        re.match(r'^\[\d+[\s~–-]+\d+\]', text)
    )

def detect_question_regions(pdf_bytes):
    """PDF에서 문항 번호를 찾아 문항별 영역 목록을 반환: [{'q_num': 1, 'regions': [(page, x0, y0, x1, y1), ...]}]"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    # 족보 PDF처럼 'N.' 뒤에 'zbN)' 표시가 있으면 그것을 우선 사용 (<조건>의 '2.' 같은 항목을 문항으로 오인하지 않게)
    all_text = "".join(page.get_text() for page in doc)
    zb_nums = {int(n) for n in re.findall(r'zb(\d{1,2})\)', all_text)}

    # 1) 본문 블록을 읽는 순서(쪽 → 왼쪽 단 → 오른쪽 단 → 위에서 아래)로 한 줄로 늘어놓음
    seq = []        # 블록 정보 dict 목록
    col_bounds = {} # (page, col) -> (x0, x1)
    page_heights = {}
    for page_num in range(len(doc)):
        page = doc[page_num]
        rect = page.rect
        mid_x = rect.width / 2.0
        page_heights[page_num] = rect.height
        header_limit = rect.height * 0.075
        footer_limit = rect.height * 0.93

        left_blocks, right_blocks = [], []
        for b in page.get_text("blocks"):
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()
            if y0 >= footer_limit or y1 <= header_limit:
                continue
            if re.match(r'^-\s*\d+\s*-$', text):
                continue
            if re.match(r'^\d+[\-~]\d+', text):
                continue
            (left_blocks if x0 < mid_x else right_blocks).append(b)

        left_x0 = max(0, min([b[0] for b in left_blocks]) - 4) if left_blocks else 0
        left_x1 = min(mid_x - 4, max([b[2] for b in left_blocks if b[2] <= mid_x + 30] or [mid_x - 5]) + 4) if left_blocks else mid_x - 5
        right_x0 = max(mid_x + 4, min([b[0] for b in right_blocks if b[0] >= mid_x - 30] or [mid_x + 5]) - 4) if right_blocks else mid_x + 5
        right_x1 = min(rect.width, max([b[2] for b in right_blocks]) + 4) if right_blocks else rect.width
        col_bounds[(page_num, 0)] = (left_x0, left_x1)
        col_bounds[(page_num, 1)] = (right_x0, right_x1)

        for col, col_blocks in enumerate([left_blocks, right_blocks]):
            for b in sorted(col_blocks, key=lambda x: x[1]):
                text = b[4].strip()
                seq.append({
                    'page': page_num, 'col': col, 'y0': b[1], 'y1': b[3], 'text': text,
                    'is_opt_5': bool(re.search(r'[⑤❺]|[\(\[]5[\)\]]|\b5[\.\)]', text)),
                })

    # 2) 문항 번호 블록(앵커) 찾기: 번호가 1씩 순서대로 이어지는 것만 인정
    anchors = []  # (seq index, q_num)
    expected_q_num = None
    for i, blk in enumerate(seq):
        match = re.match(r'^\s*(\d{1,2})\.\s*', blk['text'])
        if not match:
            continue
        cand_num = int(match.group(1))
        if expected_q_num is not None and cand_num != expected_q_num:
            continue
        has_zb = bool(re.match(r'^\s*\d{1,2}\.\s*zb' + str(cand_num) + r'\)', blk['text']))
        if zb_nums and not has_zb and cand_num in zb_nums:
            continue  # 진짜 문항은 뒤에 zb 표시와 함께 나옴
        anchors.append((i, cand_num))
        expected_q_num = cand_num + 1

    if not anchors:
        return []

    # 3) 문항마다 자기 블록 범위 정하기: 앵커 앞에 '※ 다음 글을...' 같은 지문 시작이 있으면 거기부터
    starts = []
    prev_anchor = -1
    for idx, _ in anchors:
        start = idx
        for j in range(prev_anchor + 1, idx):
            if _is_passage_start(seq[j]['text']):
                start = j
                break
        starts.append(start)
        prev_anchor = idx

    questions = []
    for k, (anchor_idx, q_num) in enumerate(anchors):
        seg_start = starts[k]
        if k + 1 < len(anchors):
            seg_end = starts[k + 1]
        else:
            # 마지막 문제는 자기 단 안에서만 (뒤쪽 정답·해설 쪽을 끌어오지 않도록)
            seg_end = anchor_idx + 1
            while seg_end < len(seq) and (seq[seg_end]['page'], seq[seg_end]['col']) == (seq[anchor_idx]['page'], seq[anchor_idx]['col']):
                seg_end += 1
        next_start = seq[seg_end] if seg_end < len(seq) else None

        # 단별로 묶어서 영역 만들기
        groups = []
        for blk in seq[seg_start:seg_end]:
            key = (blk['page'], blk['col'])
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append(blk)

        regions = []
        for (page_num, col), blks in groups:
            x0, x1 = col_bounds[(page_num, col)]
            y0 = max(0, min(b['y0'] for b in blks) - TOP_MARGIN)
            extra_margin = 4 if any(b['is_opt_5'] for b in blks) else 10
            y1 = max(b['y1'] for b in blks) + extra_margin
            if next_start is not None and (next_start['page'], next_start['col']) == (page_num, col):
                y1 = min(next_start['y0'] - 2, y1)
            else:
                y1 = min(y1, page_heights[page_num] * 0.93)
            # 너무 작은 조각(300dpi 기준 30px 이하)은 버림
            if (y1 - y0) * PDF_SAVE_DPI / 72 > 30 and (x1 - x0) * PDF_SAVE_DPI / 72 > 30:
                regions.append((page_num, x0, y0, x1, y1))

        if regions:
            questions.append({'q_num': q_num, 'regions': regions})

    return questions

def render_regions(pdf_bytes, regions, dpi=PDF_SAVE_DPI):
    """영역들을 잘라 위아래로 이어 붙인 PNG bytes"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for page_num, x0, y0, x1, y1 in regions:
        page = doc[page_num]
        clip = pymupdf.Rect(x0, y0, x1, y1) & page.rect
        if clip.is_empty:
            continue
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        parts.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    if not parts:
        parts = [Image.new("RGB", (10, 10), "white")]
    width = max(p.width for p in parts)
    out = Image.new("RGB", (width, sum(p.height for p in parts)), "white")
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()

@st.cache_data(max_entries=1000, show_spinner=False)
def render_regions_preview(pdf_bytes, regions):
    return render_regions(pdf_bytes, regions, PDF_PREVIEW_DPI)

@st.cache_data(max_entries=200, show_spinner=False)
def render_page_thumbnail(pdf_bytes, page_num, dpi=50):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    return doc[page_num].get_pixmap(dpi=dpi).tobytes("png")

def process_pdf_and_extract_questions(pdf_bytes):
    return [(q['q_num'], render_regions(pdf_bytes, tuple(q['regions']), PDF_SAVE_DPI))
            for q in detect_question_regions(pdf_bytes)]

# --- 3. Supabase DB 작업 함수들 ---
# 조회 함수는 st.cache_data로 캐시하고, 쓰기 함수는 끝에서 clear_db_cache()를 호출함.
def add_student(name):
    """성공하면 None, 실패하면 오류 메시지 반환"""
    if name in get_students():
        return "이미 등록되어 있는 학생 이름입니다."
    try:
        supabase.table("students").insert({"name": name}).execute()
        return None
    except Exception as e:
        return f"학생 등록 중 오류가 발생했습니다: {e}"
    finally:
        clear_db_cache()

@st.cache_data(ttl=DB_CACHE_TTL)
def get_students():
    res = supabase.table("students").select("name").order("name").execute()
    return [d["name"] for d in res.data] if res.data else []

@st.cache_data(ttl=DB_CACHE_TTL)
def get_students_with_wrong_counts():
    students = get_students()
    res = supabase.table("submissions").select("student_name, wrong_nums").execute()
    submissions = res.data if res.data else []
    
    counts = {name: 0 for name in students}
    for sub in submissions:
        s_name = sub.get("student_name")
        wrong_str = sub.get("wrong_nums", "")
        if s_name in counts and wrong_str:
            counts[s_name] += len([n for n in wrong_str.split(",") if n.strip()])
                
    return [(s, counts[s]) for s in students]

def delete_student(name):
    supabase.table("submissions").delete().eq("student_name", name).execute()
    supabase.table("students").delete().eq("name", name).execute()
    clear_db_cache()

def save_homework_questions(title, questions):
    """questions: [(문제 번호, PNG bytes), ...]. 저장한 숙제 id와 문항 수 반환"""
    if not questions:
        return None, 0
    
    now = now_kst_str()
    hw_res = supabase.table("homeworks").insert({"title": title, "created_at": now}).execute()
    if not hw_res.data:
        return None, 0
    
    hw_id = hw_res.data[0]["id"]
    
    q_records = []
    for q_num, img_bytes in questions:
        b64_str = base64.b64encode(img_bytes).decode("utf-8")
        q_records.append({
            "homework_id": hw_id,
            "q_num": q_num,
            "image_data": b64_str
        })
        
    supabase.table("questions").insert(q_records).execute()
    clear_db_cache()
    return hw_id, len(questions)

def save_homework_from_images(title, uploaded_files):
    now = now_kst_str()
    hw_res = supabase.table("homeworks").insert({"title": title, "created_at": now}).execute()
    if not hw_res.data:
        return None
    
    hw_id = hw_res.data[0]["id"]
    sorted_files = sorted(uploaded_files, key=natural_sort_key)
    
    q_records = []
    for idx, file in enumerate(sorted_files, start=1):
        image_bytes = file.read()
        b64_str = base64.b64encode(image_bytes).decode("utf-8")
        q_records.append({
            "homework_id": hw_id,
            "q_num": idx,
            "image_data": b64_str
        })
        
    supabase.table("questions").insert(q_records).execute()
    clear_db_cache()
    return hw_id

@st.cache_data(ttl=DB_CACHE_TTL)
def get_homeworks():
    res = supabase.table("homeworks").select("id, title, created_at").order("id", desc=True).execute()
    return [(d["id"], d["title"], d["created_at"]) for d in res.data] if res.data else []

def delete_homework(hw_id):
    supabase.table("submissions").delete().eq("homework_id", hw_id).execute()
    supabase.table("questions").delete().eq("homework_id", hw_id).execute()
    supabase.table("homeworks").delete().eq("id", hw_id).execute()
    clear_db_cache()

def update_question_image(hw_id, q_num, image_bytes):
    b64_str = base64.b64encode(image_bytes).decode("utf-8")
    supabase.table("questions").update({"image_data": b64_str}).eq("homework_id", hw_id).eq("q_num", q_num).execute()
    clear_db_cache()

def delete_question(hw_id, q_num):
    # 남은 문제 번호는 바꾸지 않음: 번호를 당기면 이미 제출된 오답 번호가 엉뚱한 문제를 가리키게 됨
    supabase.table("questions").delete().eq("homework_id", hw_id).eq("q_num", q_num).execute()
    clear_db_cache()

def save_submission(student_name, hw_id, wrong_nums_list):
    """같은 학생이 같은 숙제를 다시 제출하면 새로 추가하지 않고 기존 제출을 덮어씀. 덮어썼으면 True 반환."""
    now = now_kst_str()
    wrong_str = ",".join(map(str, sorted(wrong_nums_list)))
    existing = supabase.table("submissions").select("id").eq("student_name", student_name).eq("homework_id", hw_id).execute()
    if existing.data:
        sub_ids = [row["id"] for row in existing.data]
        supabase.table("submissions").update({
            "wrong_nums": wrong_str,
            "submitted_at": now
        }).eq("id", sub_ids[0]).execute()
        # 예전에 중복으로 쌓인 제출이 있으면 정리
        if len(sub_ids) > 1:
            supabase.table("submissions").delete().in_("id", sub_ids[1:]).execute()
        updated = True
    else:
        supabase.table("submissions").insert({
            "student_name": student_name,
            "homework_id": hw_id,
            "wrong_nums": wrong_str,
            "submitted_at": now
        }).execute()
        updated = False
    clear_db_cache()
    return updated

@st.cache_data(ttl=DB_CACHE_TTL)
def get_submitted_students():
    res = supabase.table("submissions").select("student_name").execute()
    if not res.data:
        return []
    return sorted(list(set([d["student_name"] for d in res.data])))

@st.cache_data(ttl=DB_CACHE_TTL)
def get_submissions_by_student(student_name):
    res = supabase.table("submissions").select("id, student_name, wrong_nums, submitted_at, homework_id, homeworks(title)").eq("student_name", student_name).order("id", desc=True).execute()
    data = []
    if res.data:
        for row in res.data:
            hw_title = row["homeworks"]["title"] if row.get("homeworks") and "title" in row["homeworks"] else "숙제"
            data.append((row["id"], row["student_name"], hw_title, row["wrong_nums"], row["submitted_at"], row["homework_id"]))
    return data

def delete_submission(sub_id):
    supabase.table("submissions").delete().eq("id", sub_id).execute()
    clear_db_cache()

@st.cache_data(ttl=DB_CACHE_TTL)
def get_question_nums(hw_id):
    """숙제에 실제로 저장된 문제 번호 목록 (PDF 원래 번호 그대로, 예: 18~45)"""
    res = supabase.table("questions").select("q_num").eq("homework_id", hw_id).order("q_num").execute()
    return [row["q_num"] for row in res.data] if res.data else []

@st.cache_data(ttl=DB_CACHE_TTL)
def get_question_images(hw_id):
    res = supabase.table("questions").select("q_num, image_data").eq("homework_id", hw_id).order("q_num").execute()
    return [(row["q_num"], base64.b64decode(row["image_data"])) for row in res.data] if res.data else []

def homework_labels(homeworks):
    """{숙제 id: 화면 표시 이름}. 이름이 겹치는 숙제는 등록 일시를 붙여 구분."""
    title_counts = {}
    for _, title, _ in homeworks:
        title_counts[title] = title_counts.get(title, 0) + 1
    return {hw_id: (f"{title} ({created_at})" if title_counts[title] > 1 else title)
            for hw_id, title, created_at in homeworks}

def get_wrong_questions_images(hw_id, wrong_nums_list):
    wrong_set = set(wrong_nums_list)
    return [(q_num, img) for q_num, img in get_question_images(hw_id) if q_num in wrong_set]

# --- 4. 인쇄 전용 CSS ---
st.markdown("""
    <style>
    @media print {
        [data-testid="stHeader"],
        [data-testid="stSidebar"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"],
        [data-testid="stElementToolbar"],
        [data-testid="stAppHeader"],
        header,
        footer,
        .stAppHeader,
        .stAppToolbar,
        .stTabs [role="tablist"],
        .no-print,
        [class*="st-key-noprint"],
        button,
        iframe {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
        }

        body, .stApp {
            background-color: white !important;
            color: black !important;
        }
        .main .block-container {
            padding: 0 !important;
            margin: 0 !important;
            max-width: 100% !important;
        }
        .element-container, .stColumn {
            break-inside: avoid !important;
        }
    }
    </style>
""", unsafe_allow_html=True)

# --- 5. 선생님 인증 암호 및 상태 관리 ---
# 비밀번호는 코드가 아니라 secrets에 둠 (로컬: .streamlit/secrets.toml / Streamlit Cloud: 앱 설정 > Secrets)
try:
    TEACHER_PASSWORD = st.secrets.get("TEACHER_PASSWORD")
except Exception:
    TEACHER_PASSWORD = None

# 작업 결과 메시지: st.rerun() 직전에 st.success를 띄우면 바로 사라지므로 다음 실행에서 토스트로 보여줌
def flash(msg):
    st.session_state["flash_msg"] = msg

def render_question_grid(images, key, actions=None):
    """문제 이미지 미리보기: 한 줄씩 칸을 새로 만들어 줄이 어긋나지 않게 하고, 한 줄 개수를 고를 수 있게 함"""
    per_row = st.radio("한 줄에 보기", [2, 3, 4, 5], index=2, horizontal=True,
                       format_func=lambda n: f"{n}개", key=f"grid_per_row_{key}")
    st.caption("💡 이미지에 마우스를 올리면 오른쪽 위에 나오는 ⛶ 버튼으로 크게 볼 수 있습니다.")
    for row_start in range(0, len(images), per_row):
        cols = st.columns(per_row)
        for col, (q_num, img_bytes) in zip(cols, images[row_start:row_start + per_row]):
            with col:
                with st.container(border=True):
                    st.markdown(f"**{q_num}번**")
                    st.image(img_bytes, width="stretch")
                    if actions is not None:
                        actions(q_num)

def show_flash():
    if "flash_msg" in st.session_state:
        st.toast(st.session_state.pop("flash_msg"), duration="long")

if "admin_logged_in" not in st.session_state:
    st.session_state["admin_logged_in"] = False

with st.container(key="noprint_title"):
    st.title("📚 국어 오답노트 생성 시스템")

menu_options = ["📝 [학생] 오답 체크하기", "🔒 [선생님] 관리자 모드"]
selected_menu = st.sidebar.selectbox("원하는 작업을 선택하세요", menu_options)

# --- 6. PDF 여러 개 한꺼번에 등록 (등록 전 검토·수정) ---
# 올린 PDF는 바로 저장하지 않고 st.session_state["pdf_drafts"]에 '등록 대기' 상태로 둠.
# 문항 하나 = {"regions": [(쪽, x0, y0, x1, y1), ...], "image": 직접 올린 교체 이미지 bytes 또는 None}

def default_homework_title(file_name):
    """'[기본] 2-3.문법 요소(01)_지학사...pdf' -> '문법요소(1)'. 규칙에 안 맞으면 파일 이름 그대로."""
    stem = re.sub(r'\.pdf$', '', file_name, flags=re.IGNORECASE)
    m = re.search(r'([가-힣A-Za-z][가-힣A-Za-z\s]*?)\s*\((\d+)\)', stem)
    if m:
        return f"{m.group(1).replace(' ', '')}({int(m.group(2))})"
    return stem

def make_pdf_draft(file_name, pdf_bytes):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    return {
        "id": uuid.uuid4().hex,
        "file_name": file_name,
        "title": default_homework_title(file_name),
        "pdf": pdf_bytes,
        "page_sizes": [(page.rect.width, page.rect.height) for page in doc],
        "questions": {q["q_num"]: {"regions": [tuple(r) for r in q["regions"]], "image": None}
                      for q in detect_question_regions(pdf_bytes)},
    }

def get_pdf_drafts():
    return st.session_state.setdefault("pdf_drafts", [])

def find_pdf_draft(draft_id):
    return next((d for d in get_pdf_drafts() if d["id"] == draft_id), None)

def draft_question_image(draft, q, for_save=False):
    if q["image"] is not None:
        return q["image"]
    regions = tuple(tuple(r) for r in q["regions"])
    return render_regions(draft["pdf"], regions) if for_save else render_regions_preview(draft["pdf"], regions)

def draft_problems(draft, existing_titles):
    """등록을 막는 문제(errors)와 확인해 볼 만한 점(notes)"""
    errors, notes = [], []
    title = draft["title"].strip()
    other_titles = [d["title"].strip() for d in get_pdf_drafts() if d["id"] != draft["id"]]
    if not title:
        errors.append("숙제 이름이 비어 있습니다.")
    elif title in existing_titles:
        errors.append(f"'{title}' 이름의 숙제가 이미 등록되어 있습니다.")
    elif title in other_titles:
        errors.append(f"'{title}' 이름이 이번 목록 안에 중복됩니다.")
    nums = sorted(draft["questions"])
    if not nums:
        errors.append("문제를 하나도 찾지 못했습니다. 스캔본(이미지) PDF라면 [➕ 문제 추가]로 직접 영역을 잡아 주세요.")
    else:
        missing = sorted(set(range(nums[0], nums[-1] + 1)) - set(nums))
        if missing:
            notes.append(f"빠진 번호: {', '.join(map(str, missing))}번")
        multi = [n for n in nums if draft["questions"][n]["image"] is None and len(draft["questions"][n]["regions"]) > 1]
        if multi:
            notes.append(f"다음 단·쪽으로 이어 붙인 문제: {', '.join(map(str, multi))}번 (확인 권장)")
    return errors, notes

def register_pdf_drafts(drafts):
    """대기 중인 PDF들을 숙제로 저장. 저장된 것은 목록에서 빠지고, 문제가 있는 것은 남김."""
    existing_titles = {hw[1] for hw in get_homeworks()}
    saved, failed = [], []
    progress = st.progress(0.0, text="숙제 저장 중...")
    for i, d in enumerate(drafts):
        errors, _ = draft_problems(d, existing_titles)
        if errors:
            failed.append(f"{d['title'] or d['file_name']}: {errors[0]}")
        else:
            questions = [(n, draft_question_image(d, d["questions"][n], for_save=True)) for n in sorted(d["questions"])]
            hw_id, count = save_homework_questions(d["title"].strip(), questions)
            if hw_id is None:
                failed.append(f"{d['title']}: 저장 중 오류가 발생했습니다.")
            else:
                saved.append((d["id"], d["title"].strip(), count))
                existing_titles.add(d["title"].strip())
        progress.progress((i + 1) / len(drafts), text=f"숙제 저장 중... ({i + 1}/{len(drafts)})")
    saved_ids = {sid for sid, _, _ in saved}
    st.session_state["pdf_drafts"] = [d for d in get_pdf_drafts() if d["id"] not in saved_ids]
    return saved, failed

def _pct(value, total):
    return round(value / total * 100, 1)

def _region_after(draft, region):
    """'영역 추가'의 기본값: 왼쪽 단이면 같은 쪽 오른쪽 단, 오른쪽 단이면 다음 쪽 왼쪽 단"""
    page_num, x0, y0, x1, y1 = region
    w, h = draft["page_sizes"][page_num]
    if (x0 + x1) / 2 < w / 2:
        return (page_num, w - x1, h * 0.075, w - x0, h * 0.93)
    next_page = min(page_num + 1, len(draft["page_sizes"]) - 1)
    w2, h2 = draft["page_sizes"][next_page]
    return (next_page, w2 - x1, h2 * 0.075, w2 - x0, h2 * 0.93)

def open_question_editor(draft, q_num):
    """수정 창 열기. q_num이 None이면 새 문제 추가."""
    if q_num is None:
        nums = sorted(draft["questions"])
        last = draft["questions"][nums[-1]]["regions"][-1] if nums and draft["questions"][nums[-1]]["regions"] else None
        if last is not None:
            regions = [_region_after(draft, last)]
        else:
            w, h = draft["page_sizes"][0]
            regions = [(0, w * 0.07, h * 0.075, w * 0.49, h * 0.93)]
        work = {"orig_q": None, "q_num": (nums[-1] + 1) if nums else 1, "regions": regions, "image": None}
    else:
        q = draft["questions"][q_num]
        work = {"orig_q": q_num, "q_num": q_num, "regions": list(q["regions"]), "image": q["image"]}
    work["nonce"] = 0
    st.session_state["question_editor"] = work
    question_editor_dialog(draft["id"])

@st.dialog("✏️ 문제 영역 수정", width="large")
def question_editor_dialog(draft_id):
    draft = find_pdf_draft(draft_id)
    work = st.session_state.get("question_editor")
    if draft is None or work is None:
        st.write("수정할 문제를 찾지 못했습니다.")
        return
    nonce = work["nonce"]
    page_count = len(draft["page_sizes"])

    work["q_num"] = st.number_input("문제 번호", min_value=1, max_value=999, value=work["q_num"], step=1, key=f"qe_num_{nonce}")

    st.caption("PDF에서 잘라 올 영역을 %로 정합니다. 오른쪽 쪽 그림의 빨간 상자가 지금 영역입니다. "
               "문제가 다음 단·다음 쪽으로 이어지면 [➕ 영역 추가]로 이어지는 부분을 붙이세요.")

    for i, region in enumerate(list(work["regions"])):
        page_num, x0, y0, x1, y1 = region
        with st.container(border=True):
            ctrl_col, thumb_col = st.columns([3, 2])
            with ctrl_col:
                st.markdown(f"**영역 {i + 1}**")
                new_page = st.number_input("쪽", min_value=1, max_value=page_count, value=page_num + 1, key=f"qe_page_{nonce}_{i}") - 1
                w, h = draft["page_sizes"][new_page]
                init_x = (_pct(x0, w), _pct(x1, w))
                init_y = (_pct(y0, h), _pct(y1, h))
                xs = st.slider("가로 범위 (%)", 0.0, 100.0, init_x, step=0.1, key=f"qe_x_{nonce}_{i}")
                ys = st.slider("세로 범위 (%)", 0.0, 100.0, init_y, step=0.1, key=f"qe_y_{nonce}_{i}")
                # 슬라이더를 건드리지 않았으면 원래 좌표를 그대로 둠 (반올림 때문에 영역이 미세하게 바뀌지 않게)
                nx0, nx1 = (x0, x1) if tuple(xs) == init_x else (xs[0] * w / 100, xs[1] * w / 100)
                ny0, ny1 = (y0, y1) if tuple(ys) == init_y else (ys[0] * h / 100, ys[1] * h / 100)
                work["regions"][i] = (new_page, nx0, ny0, nx1, ny1)
                if len(work["regions"]) > 1 and st.button("🗑️ 이 영역 빼기", key=f"qe_del_{nonce}_{i}"):
                    work["regions"].pop(i)
                    work["nonce"] += 1
                    st.rerun(scope="fragment")
            with thumb_col:
                thumb = Image.open(io.BytesIO(render_page_thumbnail(draft["pdf"], new_page))).convert("RGB")
                draw = ImageDraw.Draw(thumb)
                sx, sy = thumb.width / w, thumb.height / h
                for j, (p, a0, b0, a1, b1) in enumerate(work["regions"]):
                    if p == new_page:
                        draw.rectangle([a0 * sx, b0 * sy, a1 * sx, b1 * sy],
                                       outline=(229, 57, 53) if j == i else (150, 150, 150), width=3 if j == i else 2)
                st.image(thumb, caption=f"{new_page + 1}쪽", width="stretch")

    if st.button("➕ 영역 추가 (다음 단·쪽으로 이어지는 부분)", key=f"qe_add_{nonce}"):
        work["regions"].append(_region_after(draft, work["regions"][-1]))
        work["nonce"] += 1
        st.rerun(scope="fragment")

    st.markdown("---")
    up = st.file_uploader("또는 캡처한 이미지로 직접 교체", type=["png", "jpg", "jpeg"], key=f"qe_img_{nonce}")
    if up is not None:
        work["image"] = up.getvalue()
    if work["image"] is not None:
        st.info("직접 올린 이미지를 사용합니다. 위 영역 설정은 무시됩니다.")
        if st.button("↩️ 이미지 교체 취소하고 PDF 영역 사용", key=f"qe_img_clear_{nonce}"):
            work["image"] = None
            work["nonce"] += 1
            st.rerun(scope="fragment")

    st.markdown("##### 결과 미리보기")
    if work["image"] is not None:
        st.image(work["image"], width="stretch")
    else:
        st.image(render_regions_preview(draft["pdf"], tuple(tuple(r) for r in work["regions"])), width="stretch")

    save_col, cancel_col = st.columns(2)
    with save_col:
        if st.button("💾 저장", type="primary", key=f"qe_save_{nonce}", width="stretch"):
            new_num = int(work["q_num"])
            if new_num != work["orig_q"] and new_num in draft["questions"]:
                st.error(f"{new_num}번 문제가 이미 있습니다. 다른 번호를 입력해 주세요.")
            else:
                if work["orig_q"] is not None:
                    draft["questions"].pop(work["orig_q"], None)
                draft["questions"][new_num] = {"regions": list(work["regions"]), "image": work["image"]}
                draft["questions"] = dict(sorted(draft["questions"].items()))
                st.session_state.pop("question_editor", None)
                st.rerun()
    with cancel_col:
        if st.button("취소", key=f"qe_cancel_{nonce}", width="stretch"):
            st.session_state.pop("question_editor", None)
            st.rerun()

def render_pdf_draft(draft, existing_titles):
    """등록 대기 중인 PDF 하나(탭 하나)의 화면"""
    draft["title"] = st.text_input("숙제 이름", value=draft["title"], key=f"draft_title_{draft['id']}")
    st.caption(f"📄 원본 파일: {draft['file_name']} · 🧩 {len(draft['questions'])}문제")

    errors, notes = draft_problems(draft, existing_titles)
    for e in errors:
        st.error(e)
    for n in notes:
        st.warning(n)

    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("✅ 이 숙제만 등록", key=f"draft_save_{draft['id']}", disabled=bool(errors), width="stretch"):
            saved, failed = register_pdf_drafts([draft])
            if saved:
                flash(f"✅ '{saved[0][1]}' 등록 완료! ({saved[0][2]}문제)")
                st.rerun()
            for f in failed:
                st.error(f)
    with b2:
        if st.button("➕ 문제 추가", key=f"draft_addq_{draft['id']}", width="stretch"):
            open_question_editor(draft, None)
    with b3:
        if st.button("🗑️ 목록에서 빼기", key=f"draft_remove_{draft['id']}", width="stretch"):
            st.session_state["pdf_drafts"] = [d for d in get_pdf_drafts() if d["id"] != draft["id"]]
            st.rerun()

    def card_actions(q_num):
        # 카드가 좁아도 글자가 잘리지 않게 버튼을 위아래로 배치
        if st.button("✏️ 수정", key=f"draft_edit_{draft['id']}_{q_num}", width="stretch"):
            open_question_editor(draft, q_num)
        with st.popover("🗑️ 빼기", width="stretch"):
            st.write(f"{q_num}번 문제를 이 숙제에서 뺍니다.")
            if st.button("빼기 확정", type="primary", key=f"draft_delq_{draft['id']}_{q_num}"):
                draft["questions"].pop(q_num, None)
                st.rerun()

    images = [(n, draft_question_image(draft, draft["questions"][n])) for n in sorted(draft["questions"])]
    render_question_grid(images, key=f"draft_{draft['id']}", actions=card_actions)

def render_pdf_batch_upload():
    uploader_id = st.session_state.setdefault("pdf_uploader_id", 0)
    st.info("💡 PDF를 여러 개 한꺼번에 선택할 수 있습니다. [PDF 분석하기]를 누르면 문제를 자동으로 자른 뒤, "
            "PDF마다 탭이 생겨서 숙제 이름을 정하고 잘못 잘린 문제를 고친 다음 등록할 수 있습니다.")
    pdf_files = st.file_uploader("PDF 파일을 선택하세요 (여러 개 선택 가능)", type=['pdf'],
                                 accept_multiple_files=True, key=f"hw_pdfs_{uploader_id}")
    if st.button("📑 PDF 분석하기", disabled=not pdf_files):
        drafts = get_pdf_drafts()
        known = {d["file_name"] for d in drafts}
        new_files = [f for f in sorted(pdf_files, key=natural_sort_key) if f.name not in known]
        progress = st.progress(0.0, text="PDF 분석 중...")
        failed = []
        for i, f in enumerate(new_files):
            try:
                drafts.append(make_pdf_draft(f.name, f.getvalue()))
            except Exception as e:
                failed.append(f"{f.name}: {e}")
            progress.progress((i + 1) / max(1, len(new_files)), text=f"PDF 분석 중... ({i + 1}/{len(new_files)})")
        drafts.sort(key=lambda d: natural_name_key(d["file_name"]))
        st.session_state["pdf_uploader_id"] = uploader_id + 1
        skipped = len(pdf_files) - len(new_files)
        flash(f"📑 PDF {len(new_files) - len(failed)}개 분석 완료" + (f" (이미 목록에 있는 {skipped}개는 건너뜀)" if skipped else ""))
        if failed:
            st.session_state["pdf_analyze_errors"] = failed
        st.rerun()

    for err in st.session_state.pop("pdf_analyze_errors", []):
        st.error(f"PDF를 읽지 못했습니다: {err}")

    drafts = get_pdf_drafts()
    if not drafts:
        return

    existing_titles = {hw[1] for hw in get_homeworks()}
    st.markdown("---")
    st.markdown(f"### 📋 등록 대기 중인 숙제 ({len(drafts)}개)")
    summary = []
    for i, d in enumerate(drafts, start=1):
        errors, notes = draft_problems(d, existing_titles)
        mark = "❌" if errors else ("⚠️" if notes else "✅")
        summary.append(f"{mark} **{i}. {d['title'] or '(이름 없음)'}** · {len(d['questions'])}문제"
                       + (f" — {errors[0]}" if errors else ""))
    with st.expander("전체 목록 보기 (✅ 바로 등록 가능 / ⚠️ 확인 권장 / ❌ 수정 필요)", expanded=False):
        st.markdown("\n\n".join(summary))

    ok_count = sum(1 for d in drafts if not draft_problems(d, existing_titles)[0])
    c1, c2 = st.columns([2, 1])
    with c1:
        if st.button(f"✅ 등록 가능한 {ok_count}개 모두 등록", type="primary", disabled=ok_count == 0, width="stretch"):
            saved, failed = register_pdf_drafts([d for d in drafts if not draft_problems(d, existing_titles)[0]])
            msg = f"✅ 숙제 {len(saved)}개 등록 완료! (총 {sum(c for _, _, c in saved)}문제)"
            if failed:
                msg += f" / {len(failed)}개는 등록하지 못했습니다."
            flash(msg)
            st.rerun()
    with c2:
        with st.popover("🗑️ 대기 목록 전부 비우기", width="stretch"):
            st.write("등록하지 않은 PDF를 모두 목록에서 뺍니다.")
            if st.button("비우기 확정", type="primary", key="drafts_clear"):
                st.session_state["pdf_drafts"] = []
                st.rerun()

    # 탭 이름은 파일 이름 기준으로 고정 (숙제 이름을 고칠 때마다 탭이 바뀌어 선택이 풀리지 않게)
    labels = [f"{i}. {default_homework_title(d['file_name'])}" for i, d in enumerate(drafts, start=1)]
    tabs = st.tabs(labels, key=f"draft_tabs_{len(drafts)}", on_change="rerun")
    for tab, d in zip(tabs, drafts):
        if tab.open:  # 선택된 탭만 그림 (PDF가 많아도 느려지지 않게)
            with tab:
                render_pdf_draft(d, existing_titles)

# -------------------------------------------------------------
# 메뉴 1: [학생] 오답 체크하기
# -------------------------------------------------------------
if selected_menu == "📝 [학생] 오답 체크하기":
    st.subheader("📝 학생 오답 제출")
    
    students = get_students()
    homeworks = get_homeworks()
    
    if not students:
        st.warning("⚠️ 등록된 학생이 없습니다. 선생님께 학생 등록을 요청해 주세요.")
    elif not homeworks:
        st.info("등록된 숙제가 없습니다. 선생님께 문의하세요.")
    else:
        student_name = st.selectbox("👨‍🎓 이름을 선택하세요", students, key="student_name_select")
        
        # 숙제 id로 고르게 해서 이름이 같은 숙제가 하나로 합쳐지지 않게 함
        hw_labels = homework_labels(homeworks)
        selected_hw_id = st.selectbox("📖 숙제를 선택하세요", list(hw_labels.keys()), format_func=hw_labels.get)
        
        q_nums = get_question_nums(selected_hw_id)
        st.write(f"총 문항 수: **{len(q_nums)}문제**")
        
        st.markdown("---")
        st.write("👇 **틀린 문제 번호를 모두 체크해 주세요.**")
        
        # 체크박스 key에 숙제 id를 넣어서 숙제를 바꾸면 체크가 따라오지 않게 함
        wrong_answers = []
        for row_start in range(0, len(q_nums), 5):
            cols = st.columns(5)
            for j, q_num in enumerate(q_nums[row_start:row_start + 5]):
                with cols[j]:
                    if st.checkbox(f"{q_num}번", key=f"q_{selected_hw_id}_{q_num}"):
                        wrong_answers.append(q_num)
                    
        st.markdown("---")
        st.markdown(f"📊 선택한 오답 문항 수: <b style='color:#e53935; font-size:18px;'>총 {len(wrong_answers)}개</b>", unsafe_allow_html=True)
        st.write("")
        
        # 제출은 콜백에서 처리: 위젯이 그려지기 전에 실행되므로 제출 후 체크박스를 비울 수 있음
        def submit_wrong_answers(student_name, hw_id, q_nums):
            wrong = [n for n in q_nums if st.session_state.get(f"q_{hw_id}_{n}")]
            if not wrong:
                st.session_state["submit_result"] = ("warning", "틀린 문제 번호를 하나 이상 선택해 주세요.")
                return
            updated = save_submission(student_name, hw_id, wrong)
            for n in q_nums:
                st.session_state[f"q_{hw_id}_{n}"] = False
            if updated:
                msg = f"🔄 {student_name} 학생, 이전에 제출한 내용을 새 내용으로 수정했습니다! 오답 문제 (총 {len(wrong)}개): {wrong}"
            else:
                msg = f"🎉 {student_name} 학생, 제출이 완료되었습니다! 오답 문제 (총 {len(wrong)}개): {wrong}"
            st.session_state["submit_result"] = ("success", msg)

        st.button("제출하기", on_click=submit_wrong_answers, args=(student_name, selected_hw_id, q_nums))

        if "submit_result" in st.session_state:
            kind, msg = st.session_state.pop("submit_result")
            if kind == "success":
                st.success(msg)
            else:
                st.warning(msg)

# -------------------------------------------------------------
# 메뉴 2: [선생님] 관리자 모드
# -------------------------------------------------------------
elif selected_menu == "🔒 [선생님] 관리자 모드":
    if not st.session_state["admin_logged_in"]:
        st.subheader("🔒 선생님 관리자 로그인")
        st.write("선생님 전용 메뉴입니다. 비밀번호를 입력하신 후 엔터 또는 [로그인] 버튼을 눌러주세요.")

        if not TEACHER_PASSWORD:
            st.error("⚠️ 관리자 비밀번호가 설정되지 않았습니다. secrets에 TEACHER_PASSWORD를 등록해 주세요.")
        else:
            # form으로 감싸서 엔터키로도 로그인되게 함
            with st.form("admin_login_form"):
                input_password = st.text_input("비밀번호 입력", type="password", key="admin_pw_input")
                login_clicked = st.form_submit_button("로그인")
            if login_clicked:
                if input_password == TEACHER_PASSWORD:
                    st.session_state["admin_logged_in"] = True
                    flash("🔑 인증되었습니다!")
                    st.rerun()
                else:
                    st.error("❌ 비밀번호가 올바르지 않습니다. 다시 시도해 주세요.")

    else:
        show_flash()
        st.sidebar.success("🔑 관리자 로그인 완료")
        if st.sidebar.button("로그아웃"):
            st.session_state["admin_logged_in"] = False
            st.rerun()

        teacher_tab1, teacher_tab2, teacher_tab3, teacher_tab4 = st.tabs([
            "📤 새 숙제 등록",
            "📚 등록된 숙제 목록 및 개별 수정",
            "👤 학생 명단 관리",
            "🖨️ 학생별 오답노트 인쇄 및 관리"
        ])

        # --- [탭 1] 새 숙제 등록 ---
        with teacher_tab1:
            st.markdown("### 📤 새 숙제 문제 등록")
            upload_mode = st.radio("업로드 방식을 선택하세요", ["📄 PDF 업로드 (여러 개 한꺼번에 가능)", "🖼️ 이미지 파일 직접 업로드 (1.png, 2.png 등)"])

            if upload_mode.startswith("📄"):
                render_pdf_batch_upload()

            else:
                # 등록에 성공하면 form_id를 바꿔서 입력칸·첨부파일을 비움 (같은 숙제를 두 번 등록하는 실수 방지)
                form_id = st.session_state.setdefault("hw_form_id", 0)
                hw_title = st.text_input("숙제 이름 (예: 3월 2주차 문법 - 음운의 변동)", key=f"hw_title_{form_id}")
                st.info("💡 **팁**: 캡처한 이미지 파일명을 1.png, 2.png 순으로 붙여 업로드하세요.")
                uploaded_files = st.file_uploader("문제 이미지 파일들을 선택하세요", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True, key=f"hw_imgs_{form_id}")

                if st.button("이미지로 숙제 등록 완료"):
                    title = hw_title.strip()
                    if not title:
                        st.error("숙제 이름을 입력해 주세요.")
                    elif title in [hw[1] for hw in get_homeworks()]:
                        st.error(f"'{title}' 이름의 숙제가 이미 있습니다. 다른 이름을 입력해 주세요.")
                    elif not uploaded_files:
                        st.error("문제 이미지 파일을 선택해 주세요.")
                    else:
                        hw_id = save_homework_from_images(title, uploaded_files)
                        if hw_id is None:
                            st.error("숙제를 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.")
                        else:
                            st.session_state["hw_form_id"] = form_id + 1
                            st.session_state["last_registered_hw"] = hw_id
                            flash(f"✅ '{title}' 등록 완료! 총 {len(uploaded_files)}문제가 저장되었습니다.")
                            st.rerun()

                # 방금 등록한 숙제 미리보기 (다른 버튼을 눌러도 사라지지 않게 session_state에 보관)
                last_hw = st.session_state.get("last_registered_hw")
                if last_hw is not None:
                    last_labels = homework_labels([hw for hw in get_homeworks() if hw[0] == last_hw])
                    if last_labels:
                        st.markdown("---")
                        st.markdown(f"### 🔍 방금 등록한 숙제 미리보기: {last_labels[last_hw]}")
                        st.caption("잘못된 문제는 [등록된 숙제 목록 및 개별 수정] 탭에서 이미지를 교체하거나 삭제할 수 있습니다.")
                        render_question_grid(get_question_images(last_hw), key="new_hw")

        # --- [탭 2] 등록된 숙제 목록 및 개별 문제 이미지 교체/삭제 ---
        with teacher_tab2:
            st.markdown("### 📚 등록된 숙제 목록 및 개별 문제 수정")

            homeworks = get_homeworks()
            if not homeworks:
                st.info("현재 등록된 숙제가 없습니다. [새 숙제 등록] 탭에서 문제들을 먼저 올려주세요.")
            else:
                st.write(f"현재 총 **{len(homeworks)}개**의 숙제가 등록되어 있습니다.")

                # 모든 숙제의 이미지를 한꺼번에 그리면 숙제가 쌓일수록 느려지므로 하나씩 골라서 봄
                hw_labels = homework_labels(homeworks)
                hw_id = st.selectbox("📖 수정할 숙제를 선택하세요", list(hw_labels.keys()), format_func=hw_labels.get, key="manage_hw_select")
                title, created_at = next((t, c) for i, t, c in homeworks if i == hw_id)
                hw_q_nums = get_question_nums(hw_id)
                st.markdown("---")

                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown(f"#### 📖 {title}")
                    st.caption(f"🗓️ **등록 일시:** {created_at} | 🧩 **총 문항 수:** {len(hw_q_nums)}문제")
                with col2:
                    with st.popover("🗑️ 숙제 삭제"):
                        st.warning(f"'{title}' 숙제와 문제 이미지, 학생들의 제출 기록이 모두 삭제됩니다. 되돌릴 수 없습니다.")
                        if st.button("삭제 확정", type="primary", key=f"del_hw_{hw_id}"):
                            delete_homework(hw_id)
                            flash(f"🗑️ '{title}' 숙제가 삭제되었습니다.")
                            st.rerun()

                st.markdown("##### ✏️ 특정 문제 이미지 교체 또는 삭제하기")
                if not hw_q_nums:
                    st.write("저장된 문제가 없습니다.")
                else:
                    rep_col1, rep_col2, rep_col3 = st.columns([1, 2, 2])
                    with rep_col1:
                        target_q_num = st.selectbox("대상 문제 번호", hw_q_nums, format_func=lambda n: f"{n}번", key=f"num_{hw_id}")
                    with rep_col2:
                        # 교체 후 첨부파일을 비우기 위해 key를 바꿈
                        upload_id = st.session_state.setdefault(f"rep_upload_id_{hw_id}", 0)
                        new_q_img = st.file_uploader("새 문제 이미지 첨부", type=['png', 'jpg', 'jpeg'], key=f"file_{hw_id}_{upload_id}")
                    with rep_col3:
                        st.write("")
                        st.write("")
                        btn_c1, btn_c2 = st.columns(2)
                        with btn_c1:
                            if st.button("🖼️ 이미지 교체", key=f"btn_rep_{hw_id}"):
                                if new_q_img:
                                    update_question_image(hw_id, target_q_num, new_q_img.read())
                                    st.session_state[f"rep_upload_id_{hw_id}"] = upload_id + 1
                                    flash(f"✅ {target_q_num}번 문제 이미지가 새로운 이미지로 교체되었습니다!")
                                    st.rerun()
                                else:
                                    st.warning("교체할 이미지 파일을 등록해 주세요.")
                        with btn_c2:
                            with st.popover("🗑️ 이 문제 삭제"):
                                st.warning(f"{target_q_num}번 문제를 삭제합니다. 다른 문제 번호는 바뀌지 않습니다.")
                                if st.button("삭제 확정", type="primary", key=f"btn_del_q_{hw_id}"):
                                    delete_question(hw_id, target_q_num)
                                    flash(f"🗑️ {target_q_num}번 문제가 삭제되었습니다!")
                                    st.rerun()

                st.markdown("---")
                st.markdown("##### 🔍 전체 문제 이미지 미리보기")
                hw_imgs = get_question_images(hw_id)
                if hw_imgs:
                    render_question_grid(hw_imgs, key=f"hw_{hw_id}")
                else:
                    st.write("저장된 문제 이미지가 없습니다.")

        # --- [탭 3] 학생 명단 사전 등록 관리 ---
        with teacher_tab3:
            st.markdown("### 👤 학생 명단 관리")
            st.write("학생들이 오답을 제출할 때 선택할 이름 목록을 미리 등록합니다.")

            # form으로 감싸서 엔터키로 등록하고, 등록 후 입력칸을 비움
            with st.form("add_student_form", clear_on_submit=True):
                col1, col2 = st.columns([3, 1], vertical_alignment="bottom")
                with col1:
                    new_student_name = st.text_input("새 학생 이름 입력", key="new_student_input")
                with col2:
                    add_clicked = st.form_submit_button("➕ 학생 등록")
            if add_clicked:
                name = new_student_name.strip()
                if not name:
                    st.warning("학생 이름을 입력해 주세요.")
                else:
                    error = add_student(name)
                    if error:
                        st.error(error)
                    else:
                        flash(f"✅ '{name}' 학생이 등록되었습니다.")
                        st.rerun()

            st.markdown("---")

            students_with_counts = get_students_with_wrong_counts()

            list_hdr_col1, list_hdr_col2 = st.columns([2, 1])
            with list_hdr_col1:
                st.markdown("##### 📋 현재 등록된 학생 목록 (ㄱㄴㄷ순)")
            with list_hdr_col2:
                st.markdown(f"<div style='text-align: right; color: #1e88e5; font-size: 15px;'><b>총 {len(students_with_counts)}명</b> 등록됨</div>", unsafe_allow_html=True)

            if not students_with_counts:
                st.info("등록된 학생이 없습니다. 위에서 학생 이름을 추가해 주세요.")
            else:
                for s_name, wrong_count in students_with_counts:
                    s_col1, s_col2 = st.columns([3, 1])
                    with s_col1:
                        st.markdown(f"• **{s_name}** &nbsp; <span style='color: #e53935; font-size: 14px;'>(누적 오답: <b>{wrong_count}개</b>)</span>", unsafe_allow_html=True)
                    with s_col2:
                        with st.popover("🗑️ 삭제"):
                            st.warning(f"'{s_name}' 학생과 이 학생의 제출 기록이 모두 삭제됩니다.")
                            if st.button("삭제 확정", type="primary", key=f"del_student_{s_name}"):
                                delete_student(s_name)
                                flash(f"🗑️ '{s_name}' 학생이 삭제되었습니다.")
                                st.rerun()

        # --- [탭 4] 학생별 오답노트 인쇄 및 관리 ---
        with teacher_tab4:
            # 선택 상자·버튼 등은 noprint 컨테이너에 넣어 인쇄할 때 숨김
            controls = st.container(key="noprint_tab4_controls")
            with controls:
                st.markdown("### 🖨️ 학생별 오답노트 출력 및 삭제 관리")

            submitted_students = get_submitted_students()
            if not submitted_students:
                with controls:
                    st.info("아직 학생들이 제출한 오답 내역이 없습니다.")
            else:
                with controls:
                    selected_student = st.selectbox("👨‍🎓 관리할 학생을 선택하세요", submitted_students)
                student_submissions = get_submissions_by_student(selected_student)

                if not student_submissions:
                    with controls:
                        st.warning("해당 학생의 제출 내역이 없습니다.")
                else:
                    sub_by_id = {sub[0]: sub for sub in student_submissions}
                    with controls:
                        selected_sub_id = st.selectbox("📖 제출한 숙제를 선택하세요", list(sub_by_id.keys()),
                                                       format_func=lambda i: f"{sub_by_id[i][2]} (제출일시: {sub_by_id[i][4]})")
                    sub_id, student_name, hw_title, wrong_str, submitted_at, hw_id = sub_by_id[selected_sub_id]
                    wrong_list = [int(n) for n in wrong_str.split(',') if n.strip()] if wrong_str else []

                    images = get_wrong_questions_images(hw_id, wrong_list)
                    missing = sorted(set(wrong_list) - {q for q, _ in images})

                    with controls:
                        col1, col2 = st.columns([3, 1])
                        with col2:
                            with st.popover("🗑️ 이 숙제 제출 내역 삭제"):
                                st.warning(f"'{student_name}' 학생의 '{hw_title}' 제출 내역을 삭제합니다.")
                                if st.button("삭제 확정", type="primary", key=f"del_sub_{sub_id}"):
                                    delete_submission(sub_id)
                                    flash(f"🗑️ '{student_name}' 학생의 '{hw_title}' 제출 내역이 삭제되었습니다.")
                                    st.rerun()

                        if missing:
                            st.warning(f"⚠️ {', '.join(map(str, missing))}번 문제는 숙제에서 삭제되어 오답노트에 표시되지 않습니다.")

                        st.markdown("---")

                        st.iframe("""
                            <div style="text-align: center;">
                                <button onclick="window.parent.print()" style="
                                    background-color: #1e88e5;
                                    color: white;
                                    padding: 12px 28px;
                                    font-size: 16px;
                                    font-weight: bold;
                                    border: none;
                                    border-radius: 8px;
                                    cursor: pointer;
                                    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                                ">🖨️ 이 오답노트 바로 인쇄하기 (또는 PDF 저장)</button>
                            </div>
                        """, height=55)

                    st.markdown(f"""
                    <div style="text-align: center; padding: 12px 0; border-bottom: 2px solid #222; margin-bottom: 20px;">
                        <h2 style="margin: 0; font-size: 26px;">📄 맞춤 오답노트</h2>
                        <h3 style="margin: 8px 0 0 0; color: #333; font-size: 18px;">
                            학생 이름: <span style="color: #1e88e5;"><b>{student_name}</b></span> &nbsp;|&nbsp;
                            숙제명: <b>{hw_title}</b> &nbsp;|&nbsp;
                            오답 개수: <span style="color: #e53935;"><b>총 {len(wrong_list)}문제</b></span>
                        </h3>
                    </div>
                    """, unsafe_allow_html=True)

                    if images:
                        cols_print = st.columns(2)
                        for idx, (q_num, img_bytes) in enumerate(images):
                            with cols_print[idx % 2]:
                                st.image(img_bytes, width="stretch")
