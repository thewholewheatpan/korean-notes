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
    conn.commit()
    conn.close()

init_db()

# 자연스러운 파일명 정렬 함수
def natural_sort_key(file):
    numbers = re.findall(r'\d+', file.name)
    return int(numbers[0]) if numbers else file.name

# --- 2. PDF 문제 자동 자르기 (1단 & 2단, 주관식 <조건> 및 하단 여백 최적화) ---
def process_pdf_and_extract_questions(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    question_data = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        rect = page.rect
        mid_x = rect.width / 2.0
        
        # 헤더/푸터 제외 높이 영역 설정 (상위 7.5% 헤더/단원제외, 하위 7% 푸터 제외)
        header_limit = rect.height * 0.075
        footer_limit = rect.height * 0.93

        blocks = page.get_text("blocks")
        
        left_blocks = []
        right_blocks = []
        
        for b in blocks:
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()
            
            # 푸터/헤더 및 페이지 번호 형태(-1-) 무시
            if y0 >= footer_limit or y1 <= header_limit:
                continue
            if re.match(r'^-\s*\d+\s*-$', text):
                continue
            # 단원 제목 형태 ("2-3 음운의 변동", "1-2" 등) 제외
            if re.match(r'^\d+[\-~]\d+', text):
                continue
                
            col = 0 if x0 < mid_x else 1
            if col == 0:
                left_blocks.append(b)
            else:
                right_blocks.append(b)

        # 각 컬럼 내에서 y0 (위에서 아래) 순으로 정렬하여 처리
        for col, col_blocks in enumerate([left_blocks, right_blocks]):
            col_blocks.sort(key=lambda x: x[1])
            
            current_passage_y0 = None
            current_q = None
            last_q_num = None
            in_condition = False  # 주관식 <조건> 상자 내부 상태 여부

            for b in col_blocks:
                x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()

                # 단원 제목 형태 재확인 ("2-3", "1-2" 등)
                if re.match(r'^\d+[\-~]\d+', text):
                    continue

                # 주관식 <조건> 감지
                if '<조건>' in text or '[조건]' in text or text.startswith('※ 조건') or text.startswith('조건'):
                    in_condition = True

                # 문제 번호 패턴 (예: "1. ", "01. ")
                match = re.match(r'^(\d{1,2})\.\s*', text)

                # 조건(1., 2., 3.) 인 경우 문제 번호에서 예외 처리
                if match:
                    cand_q_num = int(match.group(1))
                    expected_q_num = (last_q_num + 1) if last_q_num is not None else 1

                    if in_condition:
                        # 조건 상태에서는 다음 정식 문제 번호가 아닌 경우 모두 무시
                        if cand_q_num != expected_q_num or any(term in text for term in ['할 것', '쓰시오', '포함', '지킬 것', '조건', '<', '>', '작성']):
                            match = None
                        else:
                            in_condition = False
                            last_q_num = cand_q_num
                    else:
                        if cand_q_num == expected_q_num:
                            last_q_num = cand_q_num

                # 지문/보기 시작 문구 패턴 (※, * <보기>, [1~3] 등)
                is_passage = (
                    text.startswith('※') or
                    re.match(r'^[※\*]\s*<보기>', text) or
                    re.match(r'^[※\*]\s*다음', text) or
                    re.match(r'^\[\d+[\s~–-]+\d+\]', text)
                )

                # 문제 전에 지문/보기가 먼저 나온 경우 시작 Y좌표 기록
                if is_passage and current_passage_y0 is None and match is None:
                    current_passage_y0 = y0

                # 정식 문제 번호를 만난 경우
                if match:
                    q_num = int(match.group(1))
                    
                    # 지문/보기가 먼저 있었으면 그 시작 위치부터 포함
                    start_y0 = current_passage_y0 if current_passage_y0 is not None else y0
                    current_passage_y0 = None  # 다음 문제를 위해 초기화

                    current_q = {
                        'q_num': q_num,
                        'page': page_num,
                        'col': col,
                        'y0': start_y0,
                        'max_y1': y1,
                        'page_width': rect.width,
                        'page_height': rect.height
                    }
                    question_data.append(current_q)

                # 현재 등록된 문제에 속하는 하위 텍스트 블록들의 max_y1 갱신
                elif current_q is not None:
                    current_q['max_y1'] = max(current_q['max_y1'], y1)

    # 페이지 ➔ 컬럼 ➔ Y좌표 순 정렬
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
        
        mid_pixel_x = int((q['page_width'] / 2.0) * scale_x)
        # 중앙 구분선 제거를 위한 안쪽 여백
        divider_margin = int(6 * scale_x)

        has_right_col = any(item['page'] == page_num and item['col'] == 1 for item in question_data)
        
        # 좌/우 컬럼 구분 및 구분선 안 들어가게 넓이 축소
        if has_right_col:
            if col == 0:
                crop_left = 0
                crop_right = max(0, mid_pixel_x - divider_margin)
            else:
                crop_left = min(img.width, mid_pixel_x + divider_margin)
                crop_right = img.width
        else:
            crop_left = 0
            crop_right = img.width

        # 상단 시작 위치 (-10px 여유)
        crop_top = max(0, int(q['y0'] * scale_y) - 10)
        
        # 다음 문제 위치 확인
        next_q_same_col = None
        for j in range(i + 1, len(question_data)):
            if question_data[j]['page'] == page_num and question_data[j]['col'] == col:
                next_q_same_col = question_data[j]
                break
                
        # 하단 자르기 위치 결정 (다음 문제 전 OR 실제 텍스트 끝 + 넉넉한 여백)
        if next_q_same_col:
            crop_bottom = min(img.height, int(next_q_same_col['y0'] * scale_y) - 2)
        else:
            max_content_y = min(q['max_y1'] + 35, q['page_height'] * 0.93)
            crop_bottom = min(img.height, int(max_content_y * scale_y))

        if crop_bottom > crop_top + 30 and crop_right > crop_left + 30:
            cropped_img = img.crop((crop_left, crop_top, crop_right, crop_bottom))
            img_byte_arr = io.BytesIO()
            cropped_img.save(img_byte_arr, format='PNG')
            extracted_questions.append((q_num, img_byte_arr.getvalue()))

    extracted_questions.sort(key=lambda x: x[0])
    return extracted_questions

# --- 3. DB 작업용 함수들 ---
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

def save_submission(student_name, hw_id, wrong_nums_list):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    wrong_str = ",".join(map(str, sorted(wrong_nums_list)))
    c.execute("INSERT INTO submissions (student_name, homework_id, wrong_nums, submitted_at) VALUES (?, ?, ?, ?)",
              (student_name, hw_id, wrong_str, now))
    conn.commit()
    conn.close()

def get_submissions():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute('''SELECT s.id, s.student_name, h.title, s.wrong_nums, s.submitted_at, s.homework_id 
                 FROM submissions s 
                 JOIN homeworks h ON s.homework_id = h.id 
                 ORDER BY s.id DESC''')
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

# --- 4. 인쇄용 CSS (2단 레이아웃 유지) ---
st.markdown("""
    <style>
    @media print {
        [data-testid="stSidebar"] { display: none !important; }
        header { display: none !important; }
        footer { display: none !important; }
        .stButton { display: none !important; }
        .no-print { display: none !important; }
        .element-container, .stColumn {
            break-inside: avoid;
        }
    }
    </style>
""", unsafe_allow_html=True)

# --- 5. 선생님 인증 암호 및 로그인 상태 관리 ---
TEACHER_PASSWORD = "9735"  # 👈 필요 시 선생님 비밀번호로 변경하세요.

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
    
    homeworks = get_homeworks()
    if not homeworks:
        st.info("등록된 숙제가 없습니다. 선생님께 문의하세요.")
    else:
        hw_options = {f"{hw[1]}": hw[0] for hw in homeworks}
        selected_hw_title = st.selectbox("숙제를 선택하세요", list(hw_options.keys()))
        selected_hw_id = hw_options[selected_hw_title]
        
        student_name = st.text_input("이름을 입력하세요 (예: 홍길동)")
        
        total_q = get_question_count(selected_hw_id)
        st.write(f"총 문항 수: **{total_q}문제**")
        
        st.markdown("---")
        st.write("👇 **틀린 문제 번호를 모두 체크해 주세요.**")
        
        cols = st.columns(5)
        wrong_answers = []
        for i in range(1, total_q + 1):
            with cols[(i - 1) % 5]:
                if st.checkbox(f"{i}번", key=f"q_{i}"):
                    wrong_answers.append(i)
                    
        st.markdown("---")
        if st.button("제출하기"):
            if not student_name.strip():
                st.error("이름을 입력해 주세요.")
            elif not wrong_answers:
                st.warning("틀린 문제 번호를 하나 이상 선택해 주세요.")
            else:
                save_submission(student_name.strip(), selected_hw_id, wrong_answers)
                st.success(f"🎉 {student_name} 학생, 제출이 완료되었습니다! 오답 문제: {wrong_answers}")

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

        teacher_tab1, teacher_tab2, teacher_tab3 = st.tabs([
            "📤 새 숙제 등록", 
            "📚 등록된 숙제 목록 및 삭제", 
            "🖨️ 학생별 오답노트 인쇄 및 관리"
        ])
        
        # --- [탭 1] 새 숙제 등록 ---
        with teacher_tab1:
            st.markdown("### 📤 새 숙제 문제 등록")
            hw_title = st.text_input("숙제 이름 (예: 3월 2주차 문법 - 음운의 변동)")
            
            upload_mode = st.radio("업로드 방식을 선택하세요", ["📄 PDF 자동 문제 분할 업로드", "🖼️ 이미지 파일 직접 업로드 (1.png, 2.png 등)"])
            
            if upload_mode == "📄 PDF 자동 문제 분할 업로드":
                st.info("💡 **PDF 지원 안내**: 지문/보기(※) 및 주관식 <조건>을 인식하여 문제별로 정확히 잘라냅니다.")
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
                                        st.markdown("---")
                            else:
                                st.error("PDF에서 문제 번호(1., 2. 등)를 찾지 못했습니다. 스캔본(이미지형) PDF인 경우 이미지 직접 업로드 방식을 사용해 주세요.")
            
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
        
        # --- [탭 2] 등록된 숙제 목록 및 문제 이미지 보기 ---
        with teacher_tab2:
            st.markdown("### 📚 등록된 숙제 목록 및 삭제 관리")
            
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

                    # 각 숙제별 문제 이미지 확인 (2단 레이아웃 표시)
                    with st.expander(f"🔍 '{title}' 문제 이미지 목록 보기 ({q_count}문항)"):
                        hw_imgs = get_wrong_questions_images(hw_id, list(range(1, q_count + 1)))
                        if hw_imgs:
                            img_cols = st.columns(2)
                            for idx, (q_num, img_bytes) in enumerate(hw_imgs):
                                with img_cols[idx % 2]:
                                    st.markdown(f"**📍 문제 {q_num}번**")
                                    st.image(img_bytes, use_container_width=True)
                                    st.markdown("---")
                        else:
                            st.write("저장된 문제 이미지가 없습니다.")

                    st.markdown("---")

        # --- [탭 3] 개인별 오답노트 인쇄 (2단 배치) 및 제출 내역 관리 ---
        with teacher_tab3:
            st.markdown("### 🖨️ 제출된 학생 오답노트 출력 및 삭제 관리")
            
            submissions = get_submissions()
            if not submissions:
                st.info("아직 학생들이 제출한 오답 내역이 없습니다.")
            else:
                sub_options = {f"{sub[1]} - {sub[2]} ({sub[4]})": sub for sub in submissions}
                selected_sub_title = st.selectbox("관리할 학생 제출 내역을 선택하세요", list(sub_options.keys()))
                selected_sub = sub_options[selected_sub_title]
                
                sub_id, student_name, hw_title, wrong_str, submitted_at, hw_id = selected_sub
                wrong_list = [int(n) for n in wrong_str.split(',')] if wrong_str else []
                
                col1, col2 = st.columns([3, 1])
                with col2:
                    if st.button("🗑️ 선택된 제출 내역 삭제"):
                        delete_submission(sub_id)
                        st.success(f"'{student_name}' 학생의 제출 내역이 삭제되었습니다.")
                        st.rerun()
                
                st.markdown("---")
                
                # 인쇄용 상단 헤더 (학생 이름 및 숙제 이름만 대형 강조)
                st.markdown(f"""
                <div style="text-align: center; padding: 12px 0; border-bottom: 2px solid #222; margin-bottom: 20px;">
                    <h2 style="margin: 0; font-size: 26px;">📄 맞춤 오답노트</h2>
                    <h3 style="margin: 8px 0 0 0; color: #333; font-size: 18px;">
                        학생 이름: <span style="color: #1e88e5;"><b>{student_name}</b></span> &nbsp;|&nbsp; 숙제명: <b>{hw_title}</b>
                    </h3>
                </div>
                """, unsafe_allow_html=True)
                
                images = get_wrong_questions_images(hw_id, wrong_list)
                
                # 원본 시험지처럼 오답 문제를 좌/우 2단 배치
                if images:
                    cols_print = st.columns(2)
                    for idx, (q_num, img_bytes) in enumerate(images):
                        with cols_print[idx % 2]:
                            st.markdown(f"**📍 문제 {q_num}번**")
                            st.image(img_bytes, use_container_width=True)
                            st.write("📝 **[풀이 / 정답 작성]**")
                            st.write("\n" * 2)
                            st.markdown("---")
                    
                st.markdown("<div class='no-print' style='margin-top: 20px;'><b>💡 인쇄 팁:</b> 웹브라우저에서 <b>Ctrl + P</b> (Mac은 <b>Cmd + P</b>)를 누르면 상단 헤더 및 메뉴바 없이 2단으로 구성된 오답 시험지만 깔끔하게 PDF 저장/인쇄됩니다.</div>", unsafe_allow_html=True)
