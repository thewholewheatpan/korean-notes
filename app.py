import streamlit as st
import sqlite3
import datetime
import re
import fitz  # PyMuPDF
from PIL import Image, ImageDraw
import io
import base64

# --- 1. 데이터베이스(DB) 초기화 및 컬럼 자동 확장 ---
def init_db():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS homeworks 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS questions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, homework_id INTEGER, q_num INTEGER, image_data BLOB, is_2col INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS submissions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, student_name TEXT, homework_id INTEGER, wrong_nums TEXT, submitted_at TEXT)''')
    
    # 기존 DB 호환을 위한 is_2col 컬럼 안전 추가
    try:
        c.execute("ALTER TABLE questions ADD COLUMN is_2col INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

init_db()

# 자연스러운 파일명 정렬 함수
def natural_sort_key(file):
    numbers = re.findall(r'\d+', file.name)
    return int(numbers[0]) if numbers else file.name

# 바이트 이미지를 HTML용 Base64로 변환
def bytes_to_b64(b_data):
    return "data:image/png;base64," + base64.b64encode(b_data).decode("utf-8")

# --- 2. PDF 문제 정밀 자르기 (2단 연쇄 지문 감지 포함) ---
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
            if re.match(r'^-\s*\d+\s*-$', text) or re.match(r'^\d+[\-~]\d+', text):
                continue
                
            if x0 < mid_x and x1 <= mid_x + 10:
                left_blocks.append(b)
            elif x0 >= mid_x - 10:
                right_blocks.append(b)

        col0_passage = None

        for col, col_blocks in enumerate([left_blocks, right_blocks]):
            col_blocks.sort(key=lambda x: x[1])
            current_passage_y0 = None
            current_q = None

            for b in col_blocks:
                x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()
                if re.match(r'^\d+[\-~]\d+', text):
                    continue

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
                    if col == 0:
                        col0_passage = {'y0': y0, 'y1': footer_limit}

                if is_real_question:
                    passage_info = None
                    if current_passage_y0 is not None:
                        passage_info = {'is_2col': False, 'col': col, 'y0': current_passage_y0, 'y1': y0}
                        current_passage_y0 = None
                    elif col == 1 and col0_passage is not None:
                        passage_info = {
                            'is_2col': True,
                            'col0_y0': col0_passage['y0'],
                            'col0_y1': col0_passage['y1'],
                            'col1_y0': col_blocks[0][1] if col_blocks else header_limit,
                            'col1_y1': y0
                        }
                        col0_passage = None

                    current_q = {
                        'q_num': q_num,
                        'page': page_num,
                        'col': col,
                        'y0': y0,
                        'max_y1': y1,
                        'page_width': rect.width,
                        'page_height': rect.height,
                        'passage_info': passage_info
                    }
                    question_data.append(current_q)

                elif current_q is not None:
                    current_q['max_y1'] = max(current_q['max_y1'], y1)

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

        page_blocks = page.get_text("blocks")
        q_blocks = []
        for b in page_blocks:
            bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
            if col == 0 and bx1 > (q['page_width'] / 2.0):
                continue
            if col == 1 and bx0 < (q['page_width'] / 2.0):
                continue
            if by0 >= q['y0'] - 5 and by1 <= q['max_y1'] + 10:
                q_blocks.append(b)

        if q_blocks:
            min_x = min([b[0] for b in q_blocks]) - 4
            max_x = max([b[2] for b in q_blocks]) + 4
        else:
            min_x = 0 if col == 0 else (q['page_width'] / 2.0)
            max_x = (q['page_width'] / 2.0) if col == 0 else q['page_width']

        if col == 0:
            crop_left = max(0, int(min_x * scale_x))
            crop_right = min(mid_pixel_x - 6, int(max_x * scale_x))
        else:
            crop_left = max(mid_pixel_x + 6, int(min_x * scale_x))
            crop_right = min(img.width, int(max_x * scale_x))

        p_info = q.get('passage_info')
        passage_img = None
        is_2col_spanned = False
        
        if p_info:
            if p_info.get('is_2col'):
                is_2col_spanned = True
                c0_top = max(0, int(p_info['col0_y0'] * scale_y) - 5)
                c0_bot = min(img.height, int(p_info['col0_y1'] * scale_y))
                left_p = img.crop((0, c0_top, mid_pixel_x - 6, c0_bot))

                c1_top = max(0, int(p_info['col1_y0'] * scale_y) - 5)
                c1_bot = min(img.height, int(p_info['col1_y1'] * scale_y) - 5)
                right_p = img.crop((mid_pixel_x + 6, c1_top, img.width, c1_bot))

                h = max(left_p.height, right_p.height)
                w = left_p.width + right_p.width + 12
                stitched = Image.new("RGB", (w, h), (255, 255, 255))
                stitched.paste(left_p, (0, 0))
                
                draw = ImageDraw.Draw(stitched)
                draw.line([(left_p.width + 6, 0), (left_p.width + 6, h)], fill=(200, 200, 200), width=1)
                stitched.paste(right_p, (left_p.width + 12, 0))
                passage_img = stitched
            else:
                p_top = max(0, int(p_info['y0'] * scale_y) - 5)
                p_bot = min(img.height, int(p_info['y1'] * scale_y))
                passage_img = img.crop((crop_left, p_top, crop_right, p_bot))

        crop_top = max(0, int(q['y0'] * scale_y) - 8)
        
        next_q_same_col = None
        for j in range(i + 1, len(question_data)):
            if question_data[j]['page'] == page_num and question_data[j]['col'] == col:
                next_q_same_col = question_data[j]
                break
                
        if next_q_same_col:
            crop_bottom = min(img.height, int(next_q_same_col['y0'] * scale_y) - 2)
        else:
            max_content_y = min(q['max_y1'] + 35, q['page_height'] * 0.93)
            crop_bottom = min(img.height, int(max_content_y * scale_y))

        if crop_bottom > crop_top + 20 and crop_right > crop_left + 20:
            q_body_img = img.crop((crop_left, crop_top, crop_right, crop_bottom))
            
            if passage_img:
                target_w = max(passage_img.width, q_body_img.width)
                combined_h = passage_img.height + q_body_img.height + 15
                final_img = Image.new("RGB", (target_w, combined_h), (255, 255, 255))
                final_img.paste(passage_img, (0, 0))
                final_img.paste(q_body_img, (0, passage_img.height + 15))
            else:
                final_img = q_body_img

            img_byte_arr = io.BytesIO()
            final_img.save(img_byte_arr, format='PNG')
            extracted_questions.append((q_num, img_byte_arr.getvalue(), is_2col_spanned))

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
    
    for q_num, img_bytes, is_2col in questions:
        c.execute("INSERT INTO questions (homework_id, q_num, image_data, is_2col) VALUES (?, ?, ?, ?)", 
                  (hw_id, q_num, img_bytes, 1 if is_2col else 0))
        
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
        c.execute("INSERT INTO questions (homework_id, q_num, image_data, is_2col) VALUES (?, ?, ?, 0)", 
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
    query = f"SELECT q_num, image_data, is_2col FROM questions WHERE homework_id = ? AND q_num IN ({placeholders}) ORDER BY q_num"
    c.execute(query, [hw_id] + wrong_nums_list)
    data = c.fetchall()
    conn.close()
    return data

# --- 4. 메인 화면 ---
TEACHER_PASSWORD = "1234"

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
                st.info("💡 **PDF 지원 안내**: 순차적 문제 번호를 추적하여 2단 연쇄 지문까지 자동 결합해 자릅니다.")
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
                                for idx, (q_num, img_bytes, is_2col) in enumerate(preview_images):
                                    with preview_cols[idx % 2]:
                                        st.image(img_bytes, use_container_width=True)
                                        st.markdown("---")
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
        
        # --- [탭 2] 등록된 숙제 목록 ---
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

                    with st.expander(f"🔍 '{title}' 문제 이미지 목록 보기 ({q_count}문항)"):
                        hw_imgs = get_wrong_questions_images(hw_id, list(range(1, q_count + 1)))
                        if hw_imgs:
                            img_cols = st.columns(2)
                            for idx, (q_num, img_bytes, is_2col) in enumerate(hw_imgs):
                                with img_cols[idx % 2]:
                                    st.image(img_bytes, use_container_width=True)
                                    st.markdown("---")
                        else:
                            st.write("저장된 문제 이미지가 없습니다.")

                    st.markdown("---")

        # --- [탭 3] 개인별 오답노트 인쇄 (완벽한 인쇄 영역 격리 & 2단 연쇄 지문 단독 페이지 지원) ---
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
                
                # 부모창 DOM에 CSS 격리 스타일 주입 후 인쇄 트리거
                st.components.v1.html("""
                    <script>
                    function triggerPrint() {
                        try {
                            var parentDoc = window.parent.document;
                            var styleId = 'custom-print-style-injected';
                            
                            var existingStyle = parentDoc.getElementById(styleId);
                            if (existingStyle) {
                                existingStyle.remove();
                            }
                            
                            var style = parentDoc.createElement('style');
                            style.id = styleId;
                            style.innerHTML = `
                                @media print {
                                    /* 1. 페이지 전체 요소 시각적 숨김 */
                                    body, body * {
                                        visibility: hidden !important;
                                    }

                                    /* 2. 오답노트 프린트 영역 및 그 하위 요소만 복원 */
                                    .printable-area, .printable-area * {
                                        visibility: visible !important;
                                    }

                                    /* 3. 프린트 영역을 용지 맨 꼭대기(0,0)에 강제 배치 */
                                    .printable-area {
                                        position: absolute !important;
                                        left: 0 !important;
                                        top: 0 !important;
                                        width: 100% !important;
                                        margin: 0 !important;
                                        padding: 0 !important;
                                    }

                                    /* 4. 2단 걸친 긴 지문 문제는 항상 새로운 단독 페이지로 넘김 */
                                    .page-break-before {
                                        page-break-before: always !important;
                                        break-before: page !important;
                                    }

                                    .question-card {
                                        break-inside: avoid !important;
                                        page-break-inside: avoid !important;
                                    }

                                    @page {
                                        margin: 10mm;
                                    }
                                }
                            `;
                            parentDoc.head.appendChild(style);
                        } catch(e) {
                            console.log("Print injection note:", e);
                        }
                        window.parent.print();
                    }
                    </script>
                    <div style="text-align: center;">
                        <button onclick="triggerPrint()" style="
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

                images = get_wrong_questions_images(hw_id, wrong_list)
                
                if images:
                    # HTML 기반 깔끔한 레이아웃 구성
                    html_content = f"""
                    <div class="printable-area">
                        <div style="text-align: center; padding: 12px 0; border-bottom: 2px solid #222; margin-bottom: 20px;">
                            <h2 style="margin: 0; font-size: 26px; color: #111;">📄 맞춤 오답노트</h2>
                            <h3 style="margin: 8px 0 0 0; color: #333; font-size: 18px;">
                                학생 이름: <span style="color: #1e88e5;"><b>{student_name}</b></span> &nbsp;|&nbsp; 숙제명: <b>{hw_title}</b>
                            </h3>
                        </div>
                    """
                    
                    pending_1col = []
                    
                    def render_1col_grid(items):
                        if not items:
                            return ""
                        grid_html = '<div style="display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 20px;">'
                        for q_num, img_bytes, _ in items:
                            b64 = bytes_to_b64(img_bytes)
                            grid_html += f'''
                                <div class="question-card" style="flex: 1 1 calc(50% - 15px); min-width: 45%; box-sizing: border-box;">
                                    <img src="{b64}" style="width: 100%; height: auto; display: block; border-bottom: 1px dashed #ccc; padding-bottom: 10px; margin-bottom: 10px;" />
                                </div>
                            '''
                        if len(items) % 2 == 1:
                            grid_html += '<div style="flex: 1 1 calc(50% - 15px); min-width: 45%;"></div>'
                        grid_html += '</div>'
                        return grid_html

                    for q_num, img_bytes, is_2col in images:
                        if is_2col:
                            # 2단 지문 문제 등장 시 기존 1단 모아둔 문제들을 먼저 렌더링
                            if pending_1col:
                                html_content += render_1col_grid(pending_1col)
                                pending_1col = []
                            
                            # 2단 지문 문제는 단독 새 페이지(page-break-before)에 100% 가로폭 배치
                            b64 = bytes_to_b64(img_bytes)
                            html_content += f'''
                                <div class="question-card page-break-before" style="width: 100%; margin-top: 15px; margin-bottom: 25px;">
                                    <img src="{b64}" style="width: 100%; height: auto; display: block; border-bottom: 1px dashed #ccc; padding-bottom: 10px;" />
                                </div>
                            '''
                        else:
                            pending_1col.append((q_num, img_bytes, is_2col))

                    if pending_1col:
                        html_content += render_1col_grid(pending_1col)

                    html_content += "</div>"

                    # 오답노트 화면 출력
                    st.markdown(html_content, unsafe_allow_html=True)
