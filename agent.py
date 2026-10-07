import os

from langchain_openai import ChatOpenAI
from managed_deepagents import define_deep_agent

from tools.restock import RESTOCK_TOOLS

agent = define_deep_agent(
    name="restock",
    model=ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-sol"),
        use_responses_api=True,
        model_kwargs={"parallel_tool_calls": False},
    ),
    tools=RESTOCK_TOOLS,
)
