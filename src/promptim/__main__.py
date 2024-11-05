import asyncio
import importlib.util
import json
import os
from typing import Optional

import click
import langsmith as ls
from langsmith.utils import LangSmithNotFoundError


def get_tasks(task_name: str):
    from promptim.tasks.metaprompt import metaprompt_task
    from promptim.tasks.scone import scone_task
    from promptim.tasks.simpleqa import simpleqa_task
    from promptim.tasks.ticket_classification import ticket_classification_task
    from promptim.tasks.tweet_generator import tweet_task

    tasks = {
        "scone": scone_task,
        "tweet": tweet_task,
        "metaprompt": metaprompt_task,
        "simpleqa": simpleqa_task,
        "ticket-classification": ticket_classification_task,
    }
    return tasks.get(task_name)


def load_task(name_or_path: str):
    from promptim.trainer import Task

    task = get_tasks(name_or_path)
    if task:
        return task, {}
    # If task is not in predefined tasks, try to load from file
    try:
        with open(name_or_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        evaluators_path = config["evaluators"]
        module_path, evaluators_variable = [
            part for part in evaluators_path.split(":") if part
        ]
        # First try to load it relative to the config path
        config_dir = os.path.dirname(name_or_path)
        relative_module_path = os.path.join(config_dir, module_path)
        if os.path.exists(relative_module_path):
            module_path = relative_module_path
        spec = importlib.util.spec_from_file_location("evaluators_module", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evaluators = getattr(module, evaluators_variable)
        if not isinstance(evaluators, list):
            raise ValueError(
                f"Expected evaluators to be a list, but got {type(evaluators).__name__}"
            )
        task = Task.from_dict({**config, "evaluators": evaluators})
        return task, config
    except Exception as e:
        raise ValueError(f"Could not load task from {name_or_path}: {e}")


async def run(
    task_name: str,
    batch_size: int,
    train_size: int,
    epochs: int,
    use_annotation_queue: Optional[str] = None,
    debug: bool = False,
    commit: bool = True,
):
    task, config = load_task(task_name)
    from promptim.trainer import PromptOptimizer

    optimizer = PromptOptimizer.from_config(config.get("optimizer_config", {}))

    with ls.tracing_context(project_name="Optim"):
        prompt, score = await optimizer.optimize_prompt(
            task,
            batch_size=batch_size,
            train_size=train_size,
            epochs=epochs,
            use_annotation_queue=use_annotation_queue,
            debug=debug,
        )
    if commit and task.initial_prompt.identifier is not None:
        optimizer.client.push_prompt(
            task.initial_prompt.identifier.rsplit(":", maxsplit=1)[0],
            object=prompt.load(optimizer.client),
        )

    return prompt, score


@click.group()
@click.version_option(version="1")
def cli():
    """Optimize prompts for different tasks."""
    pass


@cli.command()
@click.option(
    "--task",
    help="Task to optimize. You can pick one off the shelf or select a path to a config file. "
    "Example: 'examples/tweet_writer/config.json",
)
@click.option("--batch-size", type=int, default=40, help="Batch size for optimization")
@click.option(
    "--train-size", type=int, default=40, help="Training size for optimization"
)
@click.option("--epochs", type=int, default=2, help="Number of epochs for optimization")
@click.option("--debug", is_flag=True, help="Enable debug mode")
@click.option(
    "--use-annotation-queue",
    type=str,
    default=None,
    help="The name of the annotation queue to use. Note: we will delete the queue whenever you resume training (on every batch).",
)
@click.option(
    "--no-commit",
    is_flag=True,
    help="Do not commit the optimized prompt to the hub",
)
def train(
    task: str,
    batch_size: int,
    train_size: int,
    epochs: int,
    debug: bool,
    use_annotation_queue: Optional[str],
    no_commit: bool,
):
    """Train and optimize prompts for different tasks."""
    results = asyncio.run(
        run(
            task,
            batch_size,
            train_size,
            epochs,
            use_annotation_queue,
            debug,
            commit=not no_commit,
        )
    )
    print(results)


@cli.group()
def create():
    """Commands for creating new tasks and examples."""
    pass


@create.command("task")
@click.argument("path", type=click.Path(file_okay=False, dir_okay=True))
@click.argument("name", type=str)
@click.option("--prompt", required=True, help="Name of the prompt in LangSmith")
@click.option("--dataset", required=True, help="Name of the dataset in LangSmith")
def create_task(path: str, name: str, prompt: str, dataset: str):
    """Create a new task directory with config.json and task file for a custom prompt and dataset."""
    from langsmith import Client
    from langchain_core.prompts.structured import StructuredPrompt
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import RunnableSequence, RunnableBinding

    client = Client()
    if not client.api_key:
        raise ValueError("LANGSMITH_API_KEY required to create the task.")

    # Fetch prompt
    try:
        prompt_repo = client.get_prompt(prompt)
        chain = client.pull_prompt(prompt, include_model=True)
        if isinstance(chain, ChatPromptTemplate):
            prompt = chain
        elif isinstance(chain, RunnableSequence):
            prompt = chain.first
        else:
            raise ValueError(f"Unrecognized prompt format: {chain}")
    except Exception as e:
        raise ValueError(f"Could not fetch prompt '{prompt}': {e}")
    expected_imports = "from langchain_core.messages import AIMessage"
    expected_run_outputs_type = ""
    expected_run_outputs = 'predicted: AIMessage = run.outputs["output"]'
    if isinstance(prompt, StructuredPrompt):
        expected_imports = "from typing_extensions import TypedDict"
        try:
            properties = prompt.schema_["properties"]
            output_keys = list(properties.keys())
            type_map = {
                "string": "str",
                "integer": "int",
                "number": "float",
                "array": "list",
                "object": "dict",
                "boolean": "bool",
                "null": "None",
            }
            expected_run_outputs_type = "\n    ".join(
                f'{k.replace(" ", "_").replace("-", "_")}: {type_map.get(properties[k].get("type", ""), "Any")}'
                for k in output_keys
            )
            expected_run_outputs_type = f"""
class Outputs(TypedDict):
    {expected_run_outputs_type}
"""
            expected_run_outputs = "predicted: Outputs = run.outputs"
        except Exception:
            pass
    elif isinstance(chain.steps[1], RunnableBinding) and chain.steps[1].kwargs.get(
        "tools"
    ):
        tools = chain.steps[1].kwargs.get("tools")
        tool_names = [
            t.get("function", {}).get("name")
            for t in tools
            if t.get("function", {}).get("name")
        ]
        expected_run_outputs = f"# AI message contains optional tool_calls from your prompt\n    # Example tool names: {tool_names}\n    {expected_run_outputs}"
    identifier = prompt_repo.repo_handle

    # Fetch dataset
    if dataset.startswith("https://"):
        ds = client.clone_public_dataset(dataset)
        dataset = ds.name
    try:
        ds = client.read_dataset(dataset_name=dataset)
    except LangSmithNotFoundError:
        create_dataset = click.confirm(
            f"Dataset '{dataset}' not found. Would you like to create it?",
            default=True,
        )
        if create_dataset:
            ds = client.create_dataset(dataset_name=dataset)
            click.echo(f"Dataset '{dataset}' created successfully.")
        else:
            raise ValueError(f"Dataset '{dataset}' does not exist and was not created.")
    except Exception as e:
        raise ValueError(f"Could not fetch dataset '{dataset}': {e}") from e

    # Create task directory
    os.makedirs(path, exist_ok=True)

    # Create config.json
    config = {
        "name": name,
        "dataset": dataset,
        "evaluators": "./task.py:evaluators",
        "optimizer": {
            "model": {
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens_to_sample": 8192,
            }
        },
        "initial_prompt": {
            "identifier": identifier,
            "model_config": {"model": "claude-3-5-haiku-20241022"},
        },
    }

    with open(os.path.join(path, "config.json"), "w") as f:
        json.dump(config, f, indent=2)

    # Create task.py with placeholder evaluators
    task_template = f"""{expected_imports}
from langsmith.schemas import Run, Example

# Replace these evaluators with your own custom evaluators
# The run.outputs typically will contain one of the following:
# If you're optimizing a "StructuredPrompt" (with schema), the outputs
# should contain the extracted object
# If you're optimizing a different type of prompt, you will typically
# see the AIMessage in run.outputs['output']. E.g., you can access the text
# content in run.outputs['output'].content
# If you are defining a custom system, then you can access the output of that system directly

{expected_run_outputs_type}    
def example_evaluator(run: Run, example: Example) -> dict:
    \"\"\"An example evaluator. Larger numbers are better.\"\"\"
    {expected_run_outputs}
    # Implement your evaluation logic here
    return {{
        "key": "example_evaluator",
        "score": 1,  # Replace with actual score
        "comment": "Example evaluation. This comment instructs the LLM how to improve.",
    }}


evaluators = [example_evaluator]
"""

    with open(os.path.join(path, "task.py"), "w") as f:
        f.write(task_template.strip())

    print(f"Task '{name}' created at {path}")
    print(f"Using prompt: {prompt}")
    print(f"Using dataset: {ds.url}")
    print(
        f"Remember to implement your custom evaluators in {os.path.join(path, 'task.py')}"
    )


@create.command("example")
@click.argument("path", type=click.Path(file_okay=False, dir_okay=True))
@click.argument("name", type=str)
def create_example_task(path: str, name: str):
    """Create an example task directory with config.json, task file, and example dataset."""
    # Create example dataset
    from langsmith import Client

    client = Client()
    if not client.api_key:
        raise ValueError("LANGSMITH_API_KEY required to create the example tweet task.")
    prompt = client.pull_prompt("langchain-ai/tweet-generator-example:c39837bd")
    identifier = f"{name}-starter"
    try:
        identifier = client.push_prompt(identifier, object=prompt, tags=["starter"])
    except ValueError as e:
        try:
            client.pull_prompt_commit(identifier)

        except Exception:
            raise e
        print(f"Prompt {name}-starter already found. Continuing.")

    identifier = identifier.split("?")[0].replace(
        "https://smith.langchain.com/prompts/", ""
    )
    identifier = identifier.rsplit("/", maxsplit=1)[0]
    identifier = f"{identifier}:starter"
    try:
        dataset = client.create_dataset(name)
    except Exception as e:
        if dataset := client.read_dataset(dataset_name=name):
            pass
        else:
            raise e

    topics = [
        "NBA",
        "NFL",
        "Movies",
        "Taylor Swift",
        "Artificial Intelligence",
        "Climate Change",
        "Space Exploration",
        "Cryptocurrency",
        "Healthy Living",
        "Travel Destinations",
        "Technology",
        "Fashion",
        "Music",
        "Politics",
        "Food",
        "Education",
        "Environment",
        "Science",
        "Business",
        "Health",
    ]

    for split_name, dataset_topics in [
        ("train", topics[:10]),
        ("dev", topics[10:15]),
        ("test", topics[15:]),
    ]:
        client.create_examples(
            inputs=[{"topic": topic} for topic in dataset_topics],
            dataset_id=dataset.id,
            splits=[split_name] * len(dataset_topics),
        )

    print(f"Task directory created at {path}")
    print(f"Example dataset '{dataset.name}' created with {len(topics)} examples")
    print(f"See: {dataset.url}")
    os.makedirs(path, exist_ok=True)

    config = {
        "name": "Tweet Generator",
        "dataset": name,
        "evaluators": "./task.py:evaluators",
        "optimizer": {
            "model": {
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens_to_sample": 8192,
            }
        },
        "initial_prompt": {"identifier": identifier},
        "evaluator_descriptions": {
            "under_180_chars": "Checks if the tweet is under 180 characters. 1 if true, 0 if false.",
            "no_hashtags": "Checks if the tweet contains no hashtags. 1 if true, 0 if false.",
            "multiline": "Fails if the tweet is not multiple lines. 1 if true, 0 if false. 0 is bad.",
        },
    }

    with open(os.path.join(path, "config.json"), "w") as f:
        json.dump(config, f, indent=2)

    task_template = """
# You can replace these evaluators with your own.
# See https://docs.smith.langchain.com/evaluation/how_to_guides/evaluation/evaluate_llm_application#use-custom-evaluators
# for more information
def under_180_chars(run, example):
    \"\"\"Evaluate if the tweet is under 180 characters.\"\"\"
    result = run.outputs.get("tweet", "")
    score = int(len(result) < 180)
    comment = "Pass" if score == 1 else "Fail"
    return {
        "key": "under_180_chars",
        "score": score,
        "comment": comment,
    }

def no_hashtags(run, example):
    \"\"\"Evaluate if the tweet contains no hashtags.\"\"\"
    result = run.outputs.get("tweet", "")
    score = int("#" not in result)
    comment = "Pass" if score == 1 else "Fail"
    return {
        "key": "no_hashtags",
        "score": score,
        "comment": comment,
    }

evaluators = [multiple_lines, no_hashtags, under_180_chars]
"""

    with open(os.path.join(path, "task.py"), "w") as f:
        f.write(task_template.strip())


if __name__ == "__main__":
    cli()
