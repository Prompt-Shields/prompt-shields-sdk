import litellm
from langchain_openai import ChatOpenAI
from llama_index.core import VectorStoreIndex

llm = ChatOpenAI(model="gpt-4.1")
litellm.completion(model="mistral-large-latest", messages=[])
_ = VectorStoreIndex
