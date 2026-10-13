import os

import anthropic
import boto3
from google import genai
from openai import OpenAI

client = OpenAI(base_url="https://api.openai.com/v1?api-version=x")
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
gclient = genai.Client()

MODEL = "gpt-4o-mini"


def ask(prompt: str) -> str:
    resp = claude.messages.create(model="claude-sonnet-4-5", max_tokens=10, messages=[])
    return str(resp)


bedrock.converse(modelId="anthropic.claude-3-haiku-20240307-v1:0", messages=[])
