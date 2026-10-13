package main

import (
	"fmt"
	"os"

	openai "github.com/openai/openai-go"
)

func main() {
	key := os.Getenv("OPENAI_API_KEY")
	fmt.Println(openai.ChatModelGPT4o, key != "", "gpt-4o")
}
