const Groq = require('groq-sdk');
const dotenv = require('dotenv');
const readline = require('readline');

dotenv.config();

const groq = new Groq({ apiKey: process.env.GROQ_API_KEY });

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
});

const messages = [];

async function chat(userMessage) {
  messages.push({ role: "user", content: userMessage });

  const response = await groq.chat.completions.create({
      model: "qwen/qwen3-32b",
    messages,
  });

  const reply = response.choices[0].message.content;
  messages.push({ role: "assistant", content: reply });
  return reply;
}

function prompt() {
  rl.question("\nYou: ", async (input) => {
    const trimmed = input.trim();
    if (!trimmed || trimmed === "exit") {
      console.log("Bye!");
      rl.close();
      return;
    }

    try {
      const reply = await chat(trimmed);
      console.log(`\nGroq: ${reply}`);
    } catch (err) {
      console.error(`\nError: ${err.message}`);
    }

    prompt();
  });
}

console.log('Chat with Groq (type "exit" to quit)\n');
prompt();
