# A clean interface to upload documents, process them, and chat with the system.
# Also, allows connecting to a SQL Server database and querying it via natural language.
import streamlit as st
import os
import tempfile
from ingestion import load_documents, process_and_chunk_documents
from doc_rag import add_documents_to_es, create_qa_chain, ensure_index_exists
from db_client import SQLServerClient
from sql_rag import SQLRAG

st.set_page_config(page_title="سیستم پرسش و پاسخ هوشمند سازمانی", layout="wide")

# Custom CSS for RTL and Persian font
st.markdown(
    """
    <link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@100;200;300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <style>
    /* Vazirmatn applied to everything Streamlit renders */
    html, body, [class*="css"], .stApp {
        font-family: 'Vazirmatn', 'IranYekan', Tahoma, sans-serif !important;
    }
    .stApp {
        direction: rtl;
        text-align: right;
    }
    /* Ensure every widget/testid uses Vazirmatn */
    [data-testid="stApp"],
    [data-testid="stSidebar"],
    [data-testid="stHeader"],
    [data-testid="stTitle"],
    [data-testid="stSubheader"],
    [data-testid="stMarkdown"],
    [data-testid="stMarkdownContainer"],
    [data-testid="stWidgetLabel"],
    [data-testid="stTextInput"],
    [data-testid="stSelectbox"],
    [data-testid="stButton"],
    [data-testid="stChatMessage"],
    [data-testid="stChatInput"],
    [data-testid="stExpander"],
    [data-testid="stMetric"],
    [data-testid="stDataFrame"],
    [data-testid="stFileUploader"],
    [data-testid="stTabs"] {
        font-family: 'Vazirmatn', 'IranYekan', Tahoma, sans-serif !important;
    }
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Vazirmatn', 'IranYekan', Tahoma, sans-serif !important;
        text-align: center !important;
    }
    /* Keep code blocks monospace */
    code, pre, kbd, samp, [data-testid="stCodeBlock"] {
        font-family: 'Consolas', 'Monaco', 'Courier New', monospace !important;
    }
    /* Sidebar RTL + width */
    [data-testid="stSidebar"], .stSidebar {
        direction: rtl !important;
        text-align: right !important;
        width: 310px !important;
        min-width: 310px !important;
    }
    /* Chat / input RTL */
    .stChatMessage {
        direction: rtl;
        text-align: right;
    }
    .stChatMessage p, .stChatMessage div, .stMarkdown {
        direction: rtl;
        text-align: right;
    }
    .stButton button {
        direction: rtl;
    }
    .stChatInput {
        direction: rtl;
    }
    .stChatInput input, .stChatInput textarea, .stChatInput [contenteditable] {
        direction: rtl;
        text-align: right;
    }
    .stTextInput > div > div > input {
        direction: rtl;
        text-align: right;
    }
    /* Expander container and header stay RTL */
    .stExpander,
    .stExpander details,
    .stExpander summary,
    .stExpander summary *,
    [data-testid="stExpander"],
    [data-testid="stExpander"] details,
    [data-testid="stExpander"] summary {
        direction: rtl !important;
        text-align: right !important;
    }
    /* SQL code block -> LTR (inside expanders) */
    .stExpander pre,
    .stExpander code,
    .stExpander pre * {
        direction: ltr !important;
        text-align: left !important;
        unicode-bidi: embed !important;
    }
    /* Citation / generated records text -> LTR (inside expanders) */
    .stExpander div,
    .stExpander p,
    .stExpander span {
        direction: ltr !important;
        text-align: left !important;
    }
    /* Ensure expander header (summary) stays RTL even though inner divs are LTR */
    .stExpander summary,
    .stExpander summary *,
    .stExpander details > summary,
    .stExpander details > summary * {
        direction: rtl !important;
        text-align: right !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Initialize session state
if "messages" not in st.session_state:
    st.session_state.messages = []
if "qa_chain" not in st.session_state:
    st.session_state.qa_chain = None
if "sql_rag" not in st.session_state:
    st.session_state.sql_rag = None
if "sql_connected" not in st.session_state:
    st.session_state.sql_connected = False
if "sql_messages" not in st.session_state:
    st.session_state.sql_messages = []

st.title("سامانه بازیابی اطلاعات و پاسخ‌گویی هوشمند")

# Sidebar for Document Ingestion and Database Connection
with st.sidebar:
    st.header("بارگذاری فایل‌ها")
    uploaded_files = st.file_uploader(
        "فایل‌های PDF ،Word و Excel را آپلود کنید:", 
        type=["pdf", "docx", "xlsx", "xls"], 
        accept_multiple_files=True
    )
    
    if st.button("پردازش و نمایه‌سازی اطلاعات"):
        if not uploaded_files:
            st.warning("لطفاً ابتدا فایلی را آپلود کنید.")
        else:
            with st.spinner("در حال پردازش اطلاعات..."):
                # Save uploaded files temporarily
                temp_paths = []
                original_names = {}
                for file in uploaded_files:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.name)[1]) as tmp:
                        tmp.write(file.read())
                        temp_paths.append(tmp.name)
                        original_names[tmp.name] = file.name
                    
                # Ensure index exists
                ensure_index_exists()
                
                # Load, normalize, and chunk
                raw_docs = load_documents(temp_paths, original_names)
                chunks = process_and_chunk_documents(raw_docs)
                
                # Index to Elasticsearch
                add_documents_to_es(chunks)
                
                # Cleanup temp files
                for p in temp_paths:
                    os.remove(p)
                    
                st.success(f"{len(chunks)} تکه متن با موفقیت ایندکس شد!")
                
                # Initialize QA chain
                st.session_state.qa_chain = create_qa_chain()
    
    # Database Connection Section
    st.divider()
    st.header("اتصال به پایگاه داده")
    
    mssql_server = st.text_input("آدرس سرور", placeholder="مثال: localhost")
    mssql_port = st.number_input("پورت", placeholder="مثال: 1433", min_value=1, max_value=65535)
    mssql_database = st.text_input("نام پایگاه داده", placeholder="مثال: master")
    mssql_username = st.text_input("نام کاربری", placeholder="مثال: sa")
    mssql_password = st.text_input("رمز عبور", type="password")
    
    if st.button("اتصال به پایگاه داده"):
        with st.spinner("در حال اتصال به پایگاه داده..."):
            try:
                client = SQLServerClient(
                    server=mssql_server,
                    port=mssql_port,
                    database=mssql_database,
                    username=mssql_username,
                    password=mssql_password,
                )
                client.connect()
                if client.is_connected():
                    st.session_state.sql_rag = SQLRAG(client)
                    st.session_state.sql_connected = True
                    # st.success(f"!با موفقیت برقرار شد'{mssql_database}'اتصال به پایگاه داده")
                else:
                    st.error("اتصال به پایگاه داده برقرار نشد.")
            except Exception as e:
                st.error(f"خطا در اتصال: {str(e)}")
                st.session_state.sql_connected = False
    
    if st.session_state.sql_connected and st.session_state.sql_rag:
        st.success("پایگاه داده متصل است.")
        if st.button("قطع اتصال"):
            if st.session_state.sql_rag:
                st.session_state.sql_rag.disconnect()
            st.session_state.sql_rag = None
            st.session_state.sql_connected = False
            st.session_state.sql_messages = []

# Main Chat Area - two separated sections using tabs
doc_tab, sql_tab = st.tabs(["چت با اسناد سازمانی", "چت با پایگاه داده"])

# Tab 1: Document Q&A
with doc_tab:
    st.subheader("چت با اسناد سازمانی")

    # Flag to keep track of a question that was submitted but not answered yet
    if "doc_pending_prompt" not in st.session_state:
        st.session_state.doc_pending_prompt = None

    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # If a question was submitted in the previous run, generate the assistant answer now.
    # Generating the answer here (before the input box) keeps the input box at the bottom,
    # below the user question and the "در حال جستجو و تولید پاسخ..." / answer.
    if st.session_state.doc_pending_prompt:
        prompt = st.session_state.doc_pending_prompt
        st.session_state.doc_pending_prompt = None

        # Generate response
        with st.chat_message("assistant"):
            with st.spinner("در حال جستجو و تولید پاسخ..."):
                # Ensure QA chain is initialized
                if st.session_state.qa_chain is None:
                    try:
                        ensure_index_exists()
                        st.session_state.qa_chain = create_qa_chain()
                    except Exception as e:
                        error_msg = f"متاسفانه سیستم آماده نیست. لطفاً ابتدا اسناد خود را بارگذاری کنید. جزئیات: {str(e)}"
                        st.error(error_msg)
                        st.session_state.messages.append({"role": "assistant", "content": error_msg})
                        prompt = None

                if prompt:
                    try:
                        response = st.session_state.qa_chain.invoke({"query": prompt})
                        answer = response["result"]

                        st.markdown(answer)

                        st.session_state.messages.append({"role": "assistant", "content": answer})
                    except Exception as e:
                        error_msg = f"خطا در پردازش: {str(e)}"
                        st.error(error_msg)
                        st.session_state.messages.append({"role": "assistant", "content": error_msg})

    # Chat input for documents (always rendered at the bottom)
    prompt = st.chat_input("سوال خود را بپرسید...", key="doc_chat_input")
    if prompt:
        # Add user message to history
        st.session_state.messages.append({"role": "user", "content": prompt})
        # Mark as pending so the answer is generated after the input box on the next run
        st.session_state.doc_pending_prompt = prompt
        st.rerun()

# Tab 2: Database Q&A
with sql_tab:
    st.subheader("چت با پایگاه داده")

    if not st.session_state.sql_connected or not st.session_state.sql_rag:
        st.info("ابتدا از نوار کناری به پایگاه داده متصل شوید.")
    else:
        # Flag to keep track of a question that was submitted but not answered yet
        if "sql_pending_prompt" not in st.session_state:
            st.session_state.sql_pending_prompt = None

        # Display SQL chat history
        for message in st.session_state.sql_messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        # If a question was submitted in the previous run, generate the assistant answer now.
        # This keeps the chat input box at the bottom, below the user question and the answer.
        if st.session_state.sql_pending_prompt:
            db_prompt = st.session_state.sql_pending_prompt
            st.session_state.sql_pending_prompt = None

            # Generate response
            with st.chat_message("assistant"):
                with st.spinner("در حال تولید پاسخ از پایگاه داده..."):
                    try:
                        result = st.session_state.sql_rag.query(db_prompt)
                        answer = result["answer"]
                        citation = result["citation"]
                        sql = result["sql"]

                        # Display answer
                        st.markdown(answer)

                        # Display SQL query
                        with st.expander("نمایش کد SQL تولید شده"):
                            st.code(sql, language="sql")

                        # Display citation
                        with st.expander("منابع و نتیجه‌گیری‌ها"):
                            st.markdown(citation)

                        # Add to history
                        full_response = f"{answer}\n\n---\n**منابع:**\n{citation}"
                        st.session_state.sql_messages.append({"role": "assistant", "content": full_response})
                    except Exception as e:
                        error_msg = f"خطا در پردازش پایگاه داده: {str(e)}"
                        st.error(error_msg)
                        st.session_state.sql_messages.append({"role": "assistant", "content": error_msg})

        # Chat input for database (always rendered at the bottom)
        db_prompt = st.chat_input("سوال خود را از پایگاه داده بپرسید...", key="sql_chat_input")
        if db_prompt:
            # Add user message to history
            st.session_state.sql_messages.append({"role": "user", "content": db_prompt})
            # Mark as pending so the answer is generated after the input box on the next run
            st.session_state.sql_pending_prompt = db_prompt
            st.rerun()
