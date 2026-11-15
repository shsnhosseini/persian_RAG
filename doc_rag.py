# Set up the embedding model, connect to Elasticsearch, and build the QA chain with strict citation and refusal rules.
import os
from dotenv import load_dotenv
import requests

load_dotenv()
# from langchain_huggingface import HuggingFaceEmbeddings
# from langchain_community.embeddings import OllamaEmbeddings  # Ollama embedding strategy kept for reference
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_elasticsearch import DenseVectorStrategy, ElasticsearchStore
# from langchain_community.llms import Ollama  # kept for reference (Ollama strategy)
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_classic.chains import RetrievalQA 


# Initialize Embedding Model
# 1. Using HuggingFaceEmbeddings
# embedding_model = HuggingFaceEmbeddings(
#     model_name="intfloat/multilingual-e5-large",
#     model_kwargs={'device': 'cpu'}  # Change to 'cuda' if GPU available
# )

# 2. Using Ollama bge-m3:latest as embedding model (multilingual, strong for Persian) -- kept commented
# ollama_base_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
# if ollama_base_url.endswith("/api/embeddings"):
#     ollama_base_url = ollama_base_url[: -len("/api/embeddings")]
# embedding_model = OllamaEmbeddings(
#     model="bge-m3:latest",   # qwen3-embedding:4b; jeffh/intfloat-multilingual-e5-large-instruct:f32
#     base_url=ollama_base_url
# )

# 3. Using Google Gemini gemini-embedding-2 (online embedding model via API)
embedding_model = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-2",
    google_api_key=os.getenv("GOOGLE_API_KEY"),
)


def get_elasticsearch_store(index_name="persian_enterprise_docs"):
    """Connects to Elasticsearch and returns the vector store."""
    es_url = os.getenv("ES_URL", "http://localhost:9200")
    
    store = ElasticsearchStore(
        es_url=es_url,
        index_name=index_name,
        embedding=embedding_model,
        strategy=DenseVectorStrategy() 
    )
    return store


def ensure_index_exists(index_name="persian_enterprise_docs"):
    """Creates the index if it doesn't exist, to avoid NotFoundError."""
    es_url = os.getenv("ES_URL", "http://localhost:9200")
    
    store = get_elasticsearch_store(index_name)
    es_client = store.client
    
    if not es_client.indices.exists(index=index_name):
        embedding_dim = len(embedding_model.embed_documents(["test"])[0])
        es_client.indices.create(
            index=index_name,
            body={
                "mappings": {
                    "dynamic": True,
                    "properties": {
                        "text": {"type": "text"},
                        "embedding": {
                            "type": "dense_vector",
                            "dims": embedding_dim,
                            "index": True,
                            "similarity": "cosine"
                        },
                        "metadata": {"type": "object"}
                    }
                }
            }
        )
        print(f"Created Elasticsearch index: {index_name}")
    return es_client.indices.exists(index=index_name)

def add_documents_to_es(chunks, index_name="persian_enterprise_docs"):
    """Adds processed chunks to Elasticsearch.
    Handles partial bulk-indexing failures gracefully so a few bad documents
    don't abort indexing of the rest.
    """
    from elasticsearch.helpers import BulkIndexError

    store = get_elasticsearch_store(index_name)
    try:
        store.add_documents(chunks)
        print(f"Successfully indexed {len(chunks)} chunks into Elasticsearch.")
    except BulkIndexError as e:
        print(f"BulkIndexError: {len(e.errors)} document(s) failed to index.")
        for err in e.errors[:5]:
            print("  failed item:", err)
        # Indexing of successful documents already completed; continue.

def create_qa_chain(retrieval_k: int = 6):
    """Creates the QA chain using the existing RetrievalQA framework.

    Args:
        retrieval_k: Number of top chunks to retrieve per query. Higher values
            surface more sources for multi-source answers. Default is 6.
    """

    # ------------------------------------------------------------------
    # LLM strategy
    # ------------------------------------------------------------------
    # 1. Initialize LLM using Ollama (local model
    # llm = Ollama(model="qwen2.5:3b")  # qwen2.5:7b
    #
    # 2. ChatGoogleGenerativeAI with gemini-3.8-flash.
    # API key is read from GOOGLE_API_KEY in .env.
    # ------------------------------------------------------------------

    # Initialize LLM using Google Gemini
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",  # gemini-3.8-flash, gemini-3.6-flash
        google_api_key=os.getenv("GOOGLE_API_KEY"),
        temperature=0.1,
    )

    # Prompt: answer from the provided context only, Persian, refusal rule.
    # The context already carries real [منبع: نام فایل، صفحه: X] markers per
    # chunk (via document_prompt below), so the LLM cites from real metadata
    # instead of hallucinating file/page.
    prompt_template = """شما یک دستیار هوشمند و دقیق برای پاسخگویی به سوالات سازمانی هستید.
به سوال کاربر فقط و فقط بر اساس "متن زمینه" (Context) ارائه شده پاسخ دهید.

متن زمینه از چند تکه متنی تشکیل شده است. هر تکه در ابتدا علامت منبع خود را دارد، به این شکل:
[منبع: نام فایل، صفحه: X]

متن زمینه:
{context}

سوال کاربر:
{question}

دستورالعمل‌های بسیار مهم:
1. پاسخ را به زبان فارسی سلیس، رسمی و خوانا بنویسید.
2. فقط و فقط از اطلاعات موجود در "متن زمینه" استفاده کنید. هر واقعه/اطلاعی که از متن زمینه استخراج شده، در انتهای جمله/پاراگراف مربوطه، منبع را با همان علامت ذکر کنید (مثلاً [منبع: نام فایل، صفحه: X]).
3. اگر پاسخ نیاز به اطلاعاتی از چند منبع دارد، از همه منابع مرتبط استفاده کنید و هر منبع را با علامت جداگانهٔ خود ذکر کنید (هر منبع حداکثر یک بار در پاسخ ذکر شود).
4. اگر پاسخ سوال در "متن زمینه" وجود ندارد یا اطلاعات کافی نیست، تحت هیچ شرایطی اطلاعات خارجی اضافه نکنید و دقیقاً و فقط این جمله را برگردانید:
"متاسفانه اطلاعات کافی در اسناد موجود برای پاسخ به این سوال یافت نشد."
"""

    PROMPT = PromptTemplate(
        template=prompt_template,
        input_variables=["context", "question"]
    )

    # Each retrieved chunk is formatted with its REAL source metadata so the LLM
    # can cite the correct file and page. Metadata keys come from ingestion.py:
    #   doc.metadata["source_name"]  -> real uploaded file name (e.g. text2.docx)
    #   doc.metadata["page"]         -> 1-based page number (PDF; or real DOCX
    #                                   page via LibreOffice rendering / explicit
    #                                   Word page breaks / estimated fallback)
    document_prompt = PromptTemplate(
        input_variables=["page_content", "source_name", "page"],
        template="[منبع: {source_name}، صفحه: {page}]\n{page_content}"
    )

    store = get_elasticsearch_store()
    retriever = store.as_retriever(search_type="similarity", search_kwargs={"k": retrieval_k})

    qa_chain = RetrievalQA.from_chain_type(
        llm=llm,
        chain_type="stuff", # all k retrieved chunks are concatenated into the single {context} variable and sent to the LLM
                            # in one prompt. Increasing k directly increases the number of candidate sources the model can see and potentially cite.
        retriever=retriever,
        return_source_documents=True,
        chain_type_kwargs={
            "prompt": PROMPT,
            "document_prompt": document_prompt,
            "document_variable_name": "context"
        }
    )

    return qa_chain
