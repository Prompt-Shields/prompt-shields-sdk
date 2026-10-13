import com.anthropic.client.AnthropicClient;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;

public class Chat {
    AnthropicClient client = AnthropicOkHttpClient.fromEnv();
    String model = "claude-opus-4-1";
}
