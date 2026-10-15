import streamlit as st
import datetime
import re
import fitz  # PyMuPDF
from PIL import Image
import io
import base64
from supabase import create_client, Client

st.set_page_config(page_title="국어 오답노트 생성 시스템", page_icon="📚")

# --- 1. Supabase 연동 설정 (키는 st.secrets에서만 읽음) ---
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"].replace("/rest/v1/", "").rstrip("/")
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    TEACHER_PASSWORD = st.secrets["TEACHER_PASSWORD"]
except Exception:
    st.error(
        "⚠️ Streamlit Secrets에 SUPABASE_URL, SUPABASE_KEY, TEACHER_PASSWORD를 설정해 주세요."
    )
    st.stop()


@st.cache_resource
def init_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


supabase = init_supabase()


# 자연스러운 파일명 정렬 함수
def natural_sort_key(file):
    numbers = re.findall(r'\d+', file.name)
    return int(numbers[0]) if numbers else file.name


# --- 2. PDF 문제 자동 자르기 ---
def process_pdf_and_extract_questions(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    question_data = []

    expected_q_num = None

    for page_num in range(len(doc)):
        page = doc[page_num]
        rect = page.rect
        mid_x = rect.width / 2.0

        header_limit = rect.height * 0.075
        footer_limit = rect.height * 0.93

        blocks = page.get_text("blocks")

        left_blocks = []
        right_blocks = []

        for b in blocks:
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()

            if y0 >= footer_limit or y1 <= header_limit:
                continue
            if re.match(r'^-\s*\d+\s*-$', text):
                continue
            if re.match(r'^\d+[\-~]\d+', text):
                continue

            if x0 < mid_x:
                left_blocks.append(b)
            else:
                right_blocks.append(b)

        left_x0 = max(0, min([b[0] for b in left_blocks]) - 4) if left_blocks else 0
        left_x1 = min(mid_x - 4, max([b[2] for b in left_blocks if b[2] <= mid_x + 30] or [mid_x - 5]) + 4) if left_blocks else mid_x - 5

        right_x0 = max(mid_x + 4, min([b[0] for b in right_blocks if b[0] >= mid_x - 30] or [mid_x + 5]) - 4) if right_blocks else mid_x + 5
        right_x1 = min(rect.width, max([b[2] for b in right_blocks]) + 4) if right_blocks else rect.width

        for col, col_blocks in enumerate([left_blocks, right_blocks]):
            col_blocks.sort(key=lambda x: x[1])

            current_passage_y0 = None
            current_q = None

            for b in col_blocks:
                x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()

                if re.match(r'^\d+[\-~]\d+', text):
                    continue

                is_opt_5 = bool(re.search(r'[⑤❺]|[\(\[]5[\)\]]|\b5[\.\)]', text))

                match = re.match(r'^\s*(\d{1,2})\.\s*', text)

                is_real_question = False
                q_num = None

                if match:
                    cand_num = int(match.group(1))
                    if expected_q_num is None:
                        q_num = cand_num
                        expected_q_num = cand_num + 1
                        is_real_question = True
                    elif cand_num == expected_q_num:
                        q_num = cand_num
                        expected_q_num = cand_num + 1
                        is_real_question = True

                is_passage = (
                    text.startswith('※') or
                    re.match(r'^[※\*]\s*<보기>', text) or
                    re.match(r'^[※\*]\s*다음', text) or
                    re.match(r'^\[\d+[\s~–-]+\d+\]', text)
                )

                if is_passage and current_passage_y0 is None and not is_real_question:
                    current_passage_y0 = y0

                if is_real_question:
                    start_y0 = current_passage_y0 if current_passage_y0 is not None else y0
                    current_passage_y0 = None

                    col_x0 = left_x0 if col == 0 else right_x0
                    col_x1 = left_x1 if col == 0 else right_x1

                    current_q = {
                        'q_num': q_num,
                        'page': page_num,
                        'col': col,
                        'x0': col_x0,
                        'x1': col_x1,
                        'y0': start_y0,
                        'max_y1': y1,
                        'has_opt_5': is_opt_5,
                        'page_width': rect.width,
                        'page_height': rect.height
                    }
                    question_data.append(current_q)

                elif current_q is not None:
                    current_q['max_y1'] = max(current_q['max_y1'], y1)
                    if is_opt_5:
                        current_q['has_opt_5'] = True

    question_data.sort(key=lambda x: (x['page'], x['col'], x['y0']))
    extracted_questions = []

    for i, q in enumerate(question_data):
        q_num = q['q_num']
        page_num = q['page']
        col = q['col']
        page = doc[page_num]

        pix = page.get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))

        scale_x = img.width / q['page_width']
        scale_y = img.height / q['page_height']

        crop_left = max(0, int(q['x0'] * scale_x))
        crop_right = min(img.width, int(q['x1'] * scale_x))
        crop_top = max(0, int(q['y0'] * scale_y) - 10)

        next_q_same_col = None
        for j in range(i + 1, len(question_data)):
            if question_data[j]['page'] == page_num and question_data[j]['col'] == col:
                next_q_same_col = question_data[j]
                break

        extra_margin = 4 if q.get('has_opt_5', False) else 10
        max_content_y = q['max_y1'] + extra_margin

        if next_q_same_col:
            max_content_y = min(next_q_same_col['y0'] - 2, max_content_y)
        else:
            max_content_y = min(max_content_y, q['page_height'] * 0.93)

        crop_bottom = min(img.height, int(max_content_y * scale_y))

        if crop_bottom > crop_top + 30 and crop_right > crop_left + 30:
            cropped_img = img.crop((crop_left, crop_top, crop_right, crop_bottom))
            img_byte_arr = io.BytesIO()
            cropped_img.save(img_byte_arr, format='PNG')
            extracted_questions.append((q_num, img_byte_arr.getvalue()))

    extracted_questions.sort(key=lambda x: x[0])
    return extracted_questions


# --- 3. Supabase DB 작업 함수들 ---
# 읽기 함수는 @st.cache_data로 캐싱하고,
# 쓰기 함수는 끝에서 st.cache_data.clear()로 캐시를 비웁니다.

# ===== 쓰기 함수 =====
def add_student(name):
    try:
        supabase.table("students").insert({"name": name}).execute()
        st.cache_data.clear()
        return True
    except Exception:
        return False


def delete_student(name):
    supabase.table("students").delete().eq("name", name).execute()
    st.cache_data.clear()


def save_homework_from_pdf(title, pdf_file):
    questions = process_pdf_and_extract_questions(pdf_file.read())
    if not questions:
        return None, []

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    hw_res = supabase.table("homeworks").insert({"title": title, "created_at": now}).execute()
    if not hw_res.data:
        return None, []

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
    st.cache_data.clear()
    # 미리보기를 위해 메모리에 있는 결과를 그대로 반환 (DB 재조회 방지)
    return hw_id, questions


def save_homework_from_images(title, uploaded_files):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
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
    st.cache_data.clear()
    return hw_id


def delete_homework(hw_id):
    supabase.table("homeworks").delete().eq("id", hw_id).execute()
    st.cache_data.clear()


def update_question_image(hw_id, q_num, image_bytes):
    b64_str = base64.b64encode(image_bytes).decode("utf-8")
    supabase.table("questions").update({"image_data": b64_str}).eq("homework_id", hw_id).eq("q_num", q_num).execute()
    st.cache_data.clear()


def delete_question(hw_id, q_num):
    supabase.table("questions").delete().eq("homework_id", hw_id).eq("q_num", q_num).execute()

    res = supabase.table("questions").select("id").eq("homework_id", hw_id).order("q_num").execute()
    if res.data:
        for new_q_num, row in enumerate(res.data, start=1):
            supabase.table("questions").update({"q_num": new_q_num}).eq("id", row["id"]).execute()
    st.cache_data.clear()


def save_submission(student_name, hw_id, wrong_nums_list):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    wrong_str = ",".join(map(str, sorted(wrong_nums_list)))
    supabase.table("submissions").insert({
        "student_name": student_name,
        "homework_id": hw_id,
        "wrong_nums": wrong_str,
        "submitted_at": now
    }).execute()
    st.cache_data.clear()


def delete_submission(sub_id):
    supabase.table("submissions").delete().eq("id", sub_id).execute()
    st.cache_data.clear()


# ===== 읽기 함수 (캐싱) =====
@st.cache_data(ttl=300)
def get_students():
    res = supabase.table("students").select("name").order("name").execute()
    return [d["name"] for d in res.data] if res.data else []


@st.cache_data(ttl=120)
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


@st.cache_data(ttl=300)
def get_homeworks():
    res = supabase.table("homeworks").select("id, title, created_at").order("id", desc=True).execute()
    return [(d["id"], d["title"], d["created_at"]) for d in res.data] if res.data else []


@st.cache_data(ttl=120)
def get_submitted_students():
    res = supabase.table("submissions").select("student_name").execute()
    if not res.data:
        return []
    return sorted(list(set([d["student_name"] for d in res.data])))


@st.cache_data(ttl=120)
def get_submissions_by_student(student_name):
    res = supabase.table("submissions").select("id, student_name, wrong_nums, submitted_at, homework_id, homeworks(title)").eq("student_name", student_name).order("id", desc=True).execute()
    data = []
    if res.data:
        for row in res.data:
            hw_title = row["homeworks"]["title"] if row.get("homeworks") and "title" in row["homeworks"] else "숙제"
            data.append((row["id"], row["student_name"], hw_title, row["wrong_nums"], row["submitted_at"], row["homework_id"]))
    return data


@st.cache_data(ttl=300)
def get_question_count(hw_id):
    res = supabase.table("questions").select("id", count="exact").eq("homework_id", hw_id).execute()
    return res.count if res.count is not None else 0


@st.cache_data(ttl=600, max_entries=30, show_spinner="문제 이미지를 불러오는 중...")
def get_wrong_questions_images(hw_id, wrong_nums):
    # wrong_nums는 캐시 키로 안전하게 쓰기 위해 튜플로 받음
    if not wrong_nums:
        return []
    res = supabase.table("questions").select("q_num, image_data").eq("homework_id", hw_id).in_("q_num", list(wrong_nums)).order("q_num").execute()
    data = []
    if res.data:
        for row in res.data:
            img_bytes = base64.b64decode(row["image_data"])
            data.append((row["q_num"], img_bytes))
    return data


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
        [data-testid="stRadio"],
        header,
        footer,
        .stAppHeader,
        .stAppToolbar,
        .stTabs [role="tablist"],
        .no-print,
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

# --- 5. 선생님 인증 및 상태 관리 ---
if "admin_logged_in" not in st.session_state:
    st.session_state["admin_logged_in"] = False

st.title("📚 국어 오답노트 생성 시스템")

menu_options = ["📝 [학생] 오답 체크하기", "🔒 [선생님] 관리자 모드"]
selected_menu = st.sidebar.selectbox("원하는 작업을 선택하세요", menu_options)

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

        hw_options = {f"{hw[1]}": hw[0] for hw in homeworks}
        selected_hw_title = st.selectbox("📖 숙제를 선택하세요", list(hw_options.keys()))
        selected_hw_id = hw_options[selected_hw_title]

        total_q = get_question_count(selected_hw_id)
        st.write(f"총 문항 수: **{total_q}문제**")

        st.markdown("---")
        st.write("👇 **틀린 문제 번호를 모두 체크해 주세요.**")

        # form으로 감싸서 체크박스를 누를 때마다 재실행되지 않게 함
        # (제출 버튼을 눌렀을 때만 한 번 실행됨)
        with st.form("submit_form"):
            wrong_answers = []
            for row_start in range(1, total_q + 1, 5):
                cols = st.columns(5)
                for j in range(5):
                    q_num = row_start + j
                    if q_num <= total_q:
                        with cols[j]:
                            # 숙제별로 키를 분리해서 숙제를 바꿔도 체크 상태가 섞이지 않게 함
                            if st.checkbox(f"{q_num}번", key=f"q_{selected_hw_id}_{q_num}"):
                                wrong_answers.append(q_num)

            submitted = st.form_submit_button("제출하기")

        if submitted:
            if not wrong_answers:
                st.warning("틀린 문제 번호를 하나 이상 선택해 주세요.")
            else:
                save_submission(student_name, selected_hw_id, wrong_answers)
                st.success(f"🎉 {student_name} 학생, 제출이 완료되었습니다! 오답 문제 (총 {len(wrong_answers)}개): {wrong_answers}")

# -------------------------------------------------------------
# 메뉴 2: [선생님] 관리자 모드
# -------------------------------------------------------------
elif selected_menu == "🔒 [선생님] 관리자 모드":
    if not st.session_state["admin_logged_in"]:
        st.subheader("🔒 선생님 관리자 로그인")
        st.write("선생님 전용 메뉴입니다. 비밀번호를 입력하신 후 [로그인] 버튼을 눌러주세요.")

        input_password = st.text_input("비밀번호 입력", type="password", key="admin_pw_input")

        if st.button("로그인"):
            if input_password == TEACHER_PASSWORD:
                st.session_state["admin_logged_in"] = True
                st.success("🔑 인증되었습니다!")
                st.rerun()
            else:
                st.error("❌ 비밀번호가 올바르지 않습니다. 다시 시도해 주세요.")

    else:
        st.sidebar.success("🔑 관리자 로그인 완료")
        if st.sidebar.button("로그아웃"):
            st.session_state["admin_logged_in"] = False
            st.rerun()

        # st.tabs는 모든 탭의 코드를 매번 실행하므로, st.radio로 바꿔서
        # 선택한 메뉴의 코드만 실행되게 함
        admin_sections = [
            "📤 새 숙제 등록",
            "📚 등록된 숙제 목록 및 개별 수정",
            "👤 학생 명단 관리",
            "🖨️ 학생별 오답노트 인쇄 및 관리"
        ]
        section = st.radio("관리 메뉴", admin_sections, horizontal=True, key="admin_section")
        st.markdown("---")

        # --- [메뉴 1] 새 숙제 등록 ---
        if section == admin_sections[0]:
            st.markdown("### 📤 새 숙제 문제 등록")
            hw_title = st.text_input("숙제 이름 (예: 3월 2주차 문법 - 음운의 변동)")

            upload_mode = st.radio("업로드 방식을 선택하세요", ["📄 PDF 자동 문제 분할 업로드", "🖼️ 이미지 파일 직접 업로드 (1.png, 2.png 등)"])

            if upload_mode == "📄 PDF 자동 문제 분할 업로드":
                st.info("💡 **PDF 지원 안내**: 순차적 문제 번호를 추적하여 지문 및 주관식 <조건>까지 깔끔하게 자릅니다.")
                pdf_file = st.file_uploader("PDF 파일을 선택하세요", type=['pdf'])

                if st.button("PDF로 숙제 등록 완료"):
                    if not hw_title.strip():
                        st.error("숙제 이름을 입력해 주세요.")
                    elif not pdf_file:
                        st.error("PDF 파일을 업로드해 주세요.")
                    else:
                        with st.spinner("PDF 레이아웃 분석 및 문제 이미지 자동 분할 중..."):
                            hw_id, questions = save_homework_from_pdf(hw_title.strip(), pdf_file)
                            if questions:
                                st.success(f"✅ '{hw_title}' 등록 완료! 총 {len(questions)}문제가 자동 자르기로 저장되었습니다.")

                                st.markdown("---")
                                st.markdown("### 🔍 잘라낸 문제 이미지 전체 미리보기")
                                # DB에서 다시 내려받지 않고 메모리의 결과를 그대로 사용
                                preview_cols = st.columns(2)
                                for idx, (q_num, img_bytes) in enumerate(questions):
                                    with preview_cols[idx % 2]:
                                        st.markdown(f"**📍 문제 {q_num}번**")
                                        st.image(img_bytes, use_container_width=True)
                            else:
                                st.error("PDF에서 문제 번호를 찾지 못했습니다. 스캔본(이미지형) PDF인 경우 이미지 직접 업로드 방식을 사용해 주세요.")

            else:
                st.info("💡 **팁**: 캡처한 이미지 파일명을 1.png, 2.png 순으로 붙여 업로드하세요.")
                uploaded_files = st.file_uploader("문제 이미지 파일들을 선택하세요", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)

                if st.button("이미지로 숙제 등록 완료"):
                    if not hw_title.strip():
                        st.error("숙제 이름을 입력해 주세요.")
                    elif not uploaded_files:
                        st.error("문제 이미지 파일을 선택해 주세요.")
                    else:
                        hw_id = save_homework_from_images(hw_title.strip(), uploaded_files)
                        st.success(f"✅ '{hw_title}' 등록 완료! 총 {len(uploaded_files)}문제가 저장되었습니다.")

        # --- [메뉴 2] 등록된 숙제 목록 및 개별 문제 이미지 교체/삭제 ---
        elif section == admin_sections[1]:
            st.markdown("### 📚 등록된 숙제 목록 및 개별 문제 수정")

            homeworks = get_homeworks()
            if not homeworks:
                st.info("현재 등록된 숙제가 없습니다. [새 숙제 등록] 메뉴에서 문제들을 먼저 올려주세요.")
            else:
                st.write(f"현재 총 **{len(homeworks)}개**의 숙제가 등록되어 있습니다.")
                st.markdown("---")

                for hw_id, title, created_at in homeworks:
                    q_count = get_question_count(hw_id)
                    col1, col2 = st.columns([3, 1])

                    with col1:
                        st.markdown(f"#### 📖 {title}")
                        st.caption(f"🗓️ **등록 일시:** {created_at} | 🧩 **총 문항 수:** {q_count}문제")

                    with col2:
                        if st.button("🗑️ 숙제 삭제", key=f"del_hw_{hw_id}"):
                            delete_homework(hw_id)
                            st.success(f"'{title}' 숙제가 삭제되었습니다.")
                            st.rerun()

                    with st.expander(f"🛠️ '{title}' 문제 보기 / 개별 문제 교체 및 삭제하기"):
                        st.markdown("##### ✏️ 특정 문제 이미지 교체 또는 삭제하기")
                        rep_col1, rep_col2, rep_col3 = st.columns([1, 2, 2])

                        with rep_col1:
                            target_q_num = st.number_input("대상 문제 번호", min_value=1, max_value=max(1, q_count), value=1, key=f"num_{hw_id}")
                        with rep_col2:
                            new_q_img = st.file_uploader("새 문제 이미지 첨부", type=['png', 'jpg', 'jpeg'], key=f"file_{hw_id}")
                        with rep_col3:
                            st.write("")
                            st.write("")
                            btn_c1, btn_c2 = st.columns(2)
                            with btn_c1:
                                if st.button("🖼️ 이미지 교체", key=f"btn_rep_{hw_id}"):
                                    if new_q_img:
                                        update_question_image(hw_id, target_q_num, new_q_img.read())
                                        st.success(f"✅ {target_q_num}번 문제 이미지가 새로운 이미지로 교체되었습니다!")
                                        st.rerun()
                                    else:
                                        st.warning("교체할 이미지 파일을 등록해 주세요.")
                            with btn_c2:
                                if st.button("🗑️ 이 문제 삭제", key=f"btn_del_q_{hw_id}"):
                                    if q_count > 0:
                                        delete_question(hw_id, target_q_num)
                                        st.success(f"✅ {target_q_num}번 문제가 삭제되었습니다! (남은 문제 번호 자동 재정렬 완료)")
                                        st.rerun()
                                    else:
                                        st.error("삭제할 문제가 없습니다.")

                        st.markdown("---")

                        # 이미지는 토글을 켰을 때만 불러옴
                        if st.toggle("🔍 전체 문제 이미지 미리보기", key=f"prev_{hw_id}"):
                            hw_imgs = get_wrong_questions_images(hw_id, tuple(range(1, q_count + 1)))
                            if hw_imgs:
                                img_cols = st.columns(2)
                                for idx, (q_num, img_bytes) in enumerate(hw_imgs):
                                    with img_cols[idx % 2]:
                                        st.markdown(f"**📍 문제 {q_num}번**")
                                        st.image(img_bytes, use_container_width=True)
                            else:
                                st.write("저장된 문제 이미지가 없습니다.")

                    st.markdown("---")

        # --- [메뉴 3] 학생 명단 사전 등록 관리 ---
        elif section == admin_sections[2]:
            st.markdown("### 👤 학생 명단 관리")
            st.write("학생들이 오답을 제출할 때 선택할 이름 목록을 미리 등록합니다.")

            col1, col2 = st.columns([3, 1])
            with col1:
                new_student_name = st.text_input("새 학생 이름 입력", key="new_student_input")
            with col2:
                st.write("")
                st.write("")
                if st.button("➕ 학생 등록"):
                    if new_student_name.strip():
                        if add_student(new_student_name.strip()):
                            st.success(f"✅ '{new_student_name.strip()}' 학생이 등록되었습니다.")
                            st.rerun()
                        else:
                            st.error("이미 등록되어 있는 학생 이름입니다.")
                    else:
                        st.warning("학생 이름을 입력해 주세요.")

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
                        if st.button("🗑️ 삭제", key=f"del_student_{s_name}"):
                            delete_student(s_name)
                            st.success(f"'{s_name}' 학생이 삭제되었습니다.")
                            st.rerun()

        # --- [메뉴 4] 학생별 오답노트 인쇄 및 관리 ---
        elif section == admin_sections[3]:
            hdr_col1, hdr_col2 = st.columns([3, 1])
            with hdr_col1:
                st.markdown("### 🖨️ 학생별 오답노트 출력 및 삭제 관리")
            with hdr_col2:
                # 학생들이 새로 제출한 내역을 바로 보고 싶을 때 캐시를 비움
                if st.button("🔄 새로고침"):
                    st.cache_data.clear()
                    st.rerun()

            submitted_students = get_submitted_students()
            if not submitted_students:
                st.info("아직 학생들이 제출한 오답 내역이 없습니다.")
            else:
                selected_student = st.selectbox("👨‍🎓 관리할 학생을 선택하세요", submitted_students)

                student_submissions = get_submissions_by_student(selected_student)

                if not student_submissions:
                    st.warning("해당 학생의 제출 내역이 없습니다.")
                else:
                    sub_options = {f"{sub[2]} (제출일시: {sub[4]})": sub for sub in student_submissions}
                    selected_sub_title = st.selectbox("📖 제출한 숙제를 선택하세요", list(sub_options.keys()))
                    selected_sub = sub_options[selected_sub_title]

                    sub_id, student_name, hw_title, wrong_str, submitted_at, hw_id = selected_sub
                    wrong_list = [int(n) for n in wrong_str.split(',') if n.strip()] if wrong_str else []

                    col1, col2 = st.columns([3, 1])
                    with col2:
                        if st.button("🗑️ 이 숙제 제출 내역 삭제"):
                            delete_submission(sub_id)
                            st.success(f"'{student_name}' 학생의 '{hw_title}' 제출 내역이 삭제되었습니다.")
                            st.rerun()

                    st.markdown("---")

                    st.components.v1.html("""
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

                    images = get_wrong_questions_images(hw_id, tuple(wrong_list))

                    if images:
                        cols_print = st.columns(2)
                        for idx, (q_num, img_bytes) in enumerate(images):
                            with cols_print[idx % 2]:
                                st.image(img_bytes, use_container_width=True)
