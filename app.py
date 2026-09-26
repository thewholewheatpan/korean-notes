import streamlit as st
import sqlite3
import datetime
import re
import fitz  # PyMuPDF
from PIL import Image
import io

# --- 1. 데이터베이스(DB) 초기화 ---
def init_db():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS homeworks 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS questions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, homework_id INTEGER, q_num INTEGER, image_data BLOB)''')
    c.execute('''CREATE TABLE IF NOT EXISTS submissions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, student_name TEXT, homework_id INTEGER, wrong_nums TEXT, submitted_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS students 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)''')
    conn.commit()
    conn.close()

init_db()

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

# --- 3. DB 작업용 함수들 ---
def add_student(name):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO students (name) VALUES (?)", (name,))
        conn.commit()
        res = True
    except sqlite3.IntegrityError:
        res = False
    conn.close()
    return res

def get_students():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("SELECT name FROM students")
    data = c.fetchall()
    conn.close()
    return sorted([d[0] for d in data])

# 학생별 총 누적 오답 개수 계산 함수
def get_students_with_wrong_counts():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("SELECT name FROM students")
    students = [d[0] for d in c.fetchall()]
    
    c.execute("SELECT student_name, wrong_nums FROM submissions")
    submissions = c.fetchall()
    conn.close()
    
    counts = {name: 0 for name in students}
    for s_name, wrong_str in submissions:
        if s_name in counts:
            if wrong_str:
                counts[s_name] += len([n for n in wrong_str.split(',') if n.strip()])
                
    sorted_students = sorted(students)
    return [(s, counts[s]) for s in sorted_students]

def delete_student(name):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("DELETE FROM students WHERE name = ?", (name,))
    conn.commit()
    conn.close()

def save_homework_from_pdf(title, pdf_file):
    questions = process_pdf_and_extract_questions(pdf_file.read())
    if not questions:
        return None, 0
    
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO homeworks (title, created_at) VALUES (?, ?)", (title, now))
    hw_id = c.lastrowid
    
    for q_num, img_bytes in questions:
        c.execute("INSERT INTO questions (homework_id, q_num, image_data) VALUES (?, ?, ?)", 
                  (hw_id, q_num, img_bytes))
        
    conn.commit()
    conn.close()
    return hw_id, len(questions)

def save_homework_from_images(title, uploaded_files):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO homeworks (title, created_at) VALUES (?, ?)", (title, now))
    hw_id = c.lastrowid
    
    sorted_files = sorted(uploaded_files, key=natural_sort_key)
    for idx, file in enumerate(sorted_files, start=1):
        image_bytes = file.read()
        c.execute("INSERT INTO questions (homework_id, q_num, image_data) VALUES (?, ?, ?)", 
                  (hw_id, idx, image_bytes))
        
    conn.commit()
    conn.close()
    return hw_id

def get_homeworks():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("SELECT id, title, created_at FROM homeworks ORDER BY id DESC")
    data = c.fetchall()
    conn.close()
    return data

def delete_homework(hw_id):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("DELETE FROM homeworks WHERE id = ?", (hw_id,))
    c.execute("DELETE FROM questions WHERE homework_id = ?", (hw_id,))
    c.execute("DELETE FROM submissions WHERE homework_id = ?", (hw_id,))
    conn.commit()
    conn.close()

def update_question_image(hw_id, q_num, image_bytes):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("UPDATE questions SET image_data = ? WHERE homework_id = ? AND q_num = ?", 
              (image_bytes, hw_id, q_num))
    conn.commit()
    conn.close()

def save_submission(student_name, hw_id, wrong_nums_list):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    wrong_str = ",".join(map(str, sorted(wrong_nums_list)))
    c.execute("INSERT INTO submissions (student_name, homework_id, wrong_nums, submitted_at) VALUES (?, ?, ?, ?)",
              (student_name, hw_id, wrong_str, now))
    conn.commit()
    conn.close()

def get_submitted_students():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("SELECT DISTINCT student_name FROM submissions")
    data = c.fetchall()
    conn.close()
    return sorted([d[0] for d in data])

def get_submissions_by_student(student_name):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute('''SELECT s.id, s.student_name, h.title, s.wrong_nums, s.submitted_at, s.homework_id 
                 FROM submissions s 
                 JOIN homeworks h ON s.homework_id = h.id 
                 WHERE s.student_name = ?
                 ORDER BY s.id DESC''', (student_name,))
    data = c.fetchall()
    conn.close()
    return data

def delete_submission(sub_id):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("DELETE FROM submissions WHERE id = ?", (sub_id,))
    conn.commit()
    conn.close()

def get_question_count(hw_id):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM questions WHERE homework_id = ?", (hw_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_wrong_questions_images(hw_id, wrong_nums_list):
    if not wrong_nums_list:
        return []
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    placeholders = ','.join('?' for _ in wrong_nums_list)
    query = f"SELECT q_num, image_data FROM questions WHERE homework_id = ? AND q_num IN ({placeholders}) ORDER BY q_num"
    c.execute(query, [hw_id] + wrong_nums_list)
    data = c.fetchall()
    conn.close()
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

# --- 5. 선생님 인증 암호 및 상태 관리 ---
TEACHER_PASSWORD = "0708"

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
        
        wrong_answers = []
        for row_start in range(1, total_q + 1, 5):
            cols = st.columns(5)
            for j in range(5):
                q_num = row_start + j
                if q_num <= total_q:
                    with cols[j]:
                        if st.checkbox(f"{q_num}번", key=f"q_{q_num}"):
                            wrong_answers.append(q_num)
                    
        st.markdown("---")
        st.markdown(f"📊 선택한 오답 문항 수: <b style='color:#e53935; font-size:18px;'>총 {len(wrong_answers)}개</b>", unsafe_allow_html=True)
        st.write("")
        
        if st.button("제출하기"):
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

        teacher_tab1, teacher_tab2, teacher_tab3, teacher_tab4 = st.tabs([
            "📤 새 숙제 등록", 
            "📚 등록된 숙제 목록 및 개별 수정", 
            "👤 학생 명단 관리",
            "🖨️ 학생별 오답노트 인쇄 및 관리"
        ])
        
        # --- [탭 1] 새 숙제 등록 ---
        with teacher_tab1:
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
                            hw_id, q_count = save_homework_from_pdf(hw_title.strip(), pdf_file)
                            if q_count > 0:
                                st.success(f"✅ '{hw_title}' 등록 완료! 총 {q_count}문제가 자동 자르기로 저장되었습니다.")
                                
                                st.markdown("---")
                                st.markdown("### 🔍 잘라낸 문제 이미지 전체 미리보기")
                                preview_images = get_wrong_questions_images(hw_id, list(range(1, q_count + 1)))
                                preview_cols = st.columns(2)
                                for idx, (q_num, img_bytes) in enumerate(preview_images):
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
        
        # --- [탭 2] 등록된 숙제 목록 및 개별 문제 이미지 교체 ---
        with teacher_tab2:
            st.markdown("### 📚 등록된 숙제 목록 및 개별 문제 수정")
            
            homeworks = get_homeworks()
            if not homeworks:
                st.info("현재 등록된 숙제가 없습니다. [새 숙제 등록] 탭에서 문제들을 먼저 올려주세요.")
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
                        if st.button(f"🗑️ 숙제 삭제", key=f"del_hw_{hw_id}"):
                            delete_homework(hw_id)
                            st.success(f"'{title}' 숙제가 삭제되었습니다.")
                            st.rerun()

                    with st.expander(f"🛠️ '{title}' 문제 보기 / 잘못 잘린 특정 문제 교체하기"):
                        st.markdown("##### ✏️ 잘못 잘린 문제 교체하기")
                        rep_col1, rep_col2, rep_col3 = st.columns([1, 2, 1])
                        
                        with rep_col1:
                            target_q_num = st.number_input("수정할 문제 번호", min_value=1, max_value=q_count, value=1, key=f"num_{hw_id}")
                        with rep_col2:
                            new_q_img = st.file_uploader("새 문제 이미지 첨부", type=['png', 'jpg', 'jpeg'], key=f"file_{hw_id}")
                        with rep_col3:
                            st.write("")
                            st.write("")
                            if st.button("이 문제 이미지 교체", key=f"btn_rep_{hw_id}"):
                                if new_q_img:
                                    update_question_image(hw_id, target_q_num, new_q_img.read())
                                    st.success(f"✅ {target_q_num}번 문제 이미지가 새로운 이미지로 교체되었습니다!")
                                    st.rerun()
                                else:
                                    st.warning("교체할 이미지 파일을 등록해 주세요.")

                        st.markdown("---")
                        
                        st.markdown("##### 🔍 전체 문제 이미지 미리보기")
                        hw_imgs = get_wrong_questions_images(hw_id, list(range(1, q_count + 1)))
                        if hw_imgs:
                            img_cols = st.columns(2)
                            for idx, (q_num, img_bytes) in enumerate(hw_imgs):
                                with img_cols[idx % 2]:
                                    st.markdown(f"**📍 문제 {q_num}번**")
                                    st.image(img_bytes, use_container_width=True)
                        else:
                            st.write("저장된 문제 이미지가 없습니다.")

                    st.markdown("---")

        # --- [탭 3] 학생 명단 사전 등록 관리 (총 누적 오답 개수 표시 반영) ---
        with teacher_tab3:
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

        # --- [탭 4] 학생별 오답노트 인쇄 및 관리 ---
        with teacher_tab4:
            st.markdown("### 🖨️ 학생별 오답노트 출력 및 삭제 관리")
            
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
                    wrong_list = [int(n) for n in wrong_str.split(',')] if wrong_str else []
                    
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
                    
                    images = get_wrong_questions_images(hw_id, wrong_list)
                    
                    if images:
                        cols_print = st.columns(2)
                        for idx, (q_num, img_bytes) in enumerate(images):
                            with cols_print[idx % 2]:
                                st.image(img_bytes, use_container_width=True)
