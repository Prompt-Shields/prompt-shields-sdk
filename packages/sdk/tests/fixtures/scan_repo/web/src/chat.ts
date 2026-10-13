import Anthropic from "@anthropic-ai/sdk";
import { BedrockRuntimeClient } from "@aws-sdk/client-bedrock-runtime";
const OpenAI = require("openai");

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const bedrock = new BedrockRuntimeClient({ region: "us-east-1" });
const model = "gemini-2.5-flash";
const url = "https://generativelanguage.googleapis.com/v1beta/models";
export { anthropic, bedrock, model, url, OpenAI };
