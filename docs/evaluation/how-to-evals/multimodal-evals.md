# Multimodal Evals

## Multimodal Templates

Multimodal evaluation templates enable users to evaluate tasks involving multiple input or output modalities, such as text, audio, or images. These templates provide a structured framework for constructing evaluation prompts, allowing LLMs to assess the quality, correctness, or relevance of outputs across diverse use cases.&#x20;

The flexibility of multimodal templates makes them applicable to a wide range of scenarios, such as:

* Evaluating **emotional tone** in audio inputs, such as detecting user frustration or anger.
* Assessing the quality of **image captioning** tasks.
* Judging tasks that combine **image and text inputs** to produce contextualized outputs.

These examples illustrate how multimodal templates can be applied, but their versatility supports a broad spectrum of evaluation tasks tailored to specific user needs.

### **ClassificationTemplate**

`ClassificationTemplate` is a class used to create evaluation prompts that are more complex than a simple string for classification tasks. We can also build prompts that consist of multiple message parts. We may include text, audio, or images in these messages, enabling us to construct multimodal evals if the LLM supports multimodal inputs.

By defining a `ClassificationTemplate`  we can construct multi-part and multimodal evaluation templates by combining multiple `PromptPartTemplate`objects.

```python
from phoenix.evals.templates import (
    ClassificationTemplate,
    PromptPartTemplate,
)
```

* An evaluation prompt can consist of multiple **PromptPartTemplate** objects
* Each **PromptPartTemplate** can have a different content type
* Combine multiple **PromptPartTemplate** with templating variables to evaluate audio or image inputs

#### **Structure of a ClassificationTemplate**

A `ClassificationTemplate` consists of the following components:

1. **Rails**: These are the allowed classification labels for this evaluation task
2. **Template**: A list of `PromptPartTemplate` objects specifying the structure of the evaluation input. Each `PromptPartTemplate` includes:
   * **content\_type**: The type of content (e.g., `TEXT`, `AUDIO`, `IMAGE`).
   * **template**: The string or object defining the content for that part.
3. **Explanation\_Template** _(optional)_: This is a separate structure used to generate explanations if explanations are enabled via `llm_classify`. If not enabled, this component is ignored.

#### **Example: Intent Classification in Audio**

The following example demonstrates how to create a `ClassificationTemplate` for an intent detection eval for a voice application:

```python
from phoenix.evals.templates import (
    ClassificationTemplate,
    PromptPartContentType,
    PromptPartTemplate,
)

# Define valid classification labels (rails)
TONE_EMOTION_RAILS = ["positive", "neutral", "negative"]

# Create the classification template
template = ClassificationTemplate(
    rails=TONE_EMOTION_RAILS,  # Specify the valid output labels
    template=[
        # Prompt part 1: Task description
        PromptPartTemplate(
            content_type=PromptPartContentType.TEXT,
            template="""
            You are a helpful AI bot that checks for the tone of the audio.
            Analyze the audio file and determine the tone (e.g., positive, neutral, negative).
            Your evaluation should provide a multiclass label from the following options: ['positive', 'neutral', 'negative'].
            
            Here is the audio:
            """,
        ),
        # Prompt part 2: Insert the audio data
        PromptPartTemplate(
            content_type=PromptPartContentType.AUDIO,
            template="{audio}",  # Placeholder for the audio content
        ),
        # Prompt part 3: Define the response format
        PromptPartTemplate(
            content_type=PromptPartContentType.TEXT,
            template="""
            Your response must be a string, either positive, neutral, or negative, and should not contain any text or characters aside from that.
            """,
        ),
    ],
)
```

#### **Adapting to Different Modalities**

The flexibility of `ClassificationTemplate` allows users to adapt it for various modalities, such as:

* **Image Inputs**: Replace `PromptPartContentType.AUDIO` with `PromptPartContentType.IMAGE` and update the templates accordingly.
* **Mixed Modalities**: Combine `TEXT`, `AUDIO`, and `IMAGE` for multimodal tasks requiring contextualized inputs.

## **Running the Evaluation with `llm_classify`**

The `llm_classify` function can be used to run multimodal evaluations. This function supports input in the following formats:

* **DataFrame**: A DataFrame containing audio or image URLs, base64-encoded strings, and any additional data required for the evaluation.
* **List**: A collection of data items (e.g., audio or image URLs, list of base64 encoded strings).

### **Key Considerations for Input Data**

* **Public Links**: If the data contains URLs for audio or image inputs, they must be publicly accessible for OpenAI to process them directly.
* **Base64-Encoding**: For private or local data, users must encode audio or image files as base64 strings and pass them to the function.
* **Data Processor** _(optional)_: If links are not public and require transformation (e.g., base64 encoding), a data processor can be passed directly to `llm_classify` to handle the conversion in parallel, ensuring secure and efficient processing.

### **Using a Data Processor**

A data processor enables efficient parallel processing of private or raw data into the required format.

**Requirements**

1. **Consistent Input/Output**: Input and output types should match, e.g., a series to a series for DataFrame processing.
2. **Link Handling**: Fetch data from provided links (e.g., cloud storage) and encode it in base64.
3. **Column Consistency**: The processed data must align with the columns referenced in the template.

{% hint style="info" %}
Note: The data processor processes individual rows or items at a time:

* When using a DataFrame, the processor should handle one **series** (row) at a time.
* When using a list, the processor should handle one **string** (item) at a time.
{% endhint %}

**Example: Processing Audio Links**

The following is an example of a data processor that fetches audio from Google Cloud Storage, encodes it as base64, and assigns it to the appropriate column:

```python
async def async_fetch_gcloud_data(row: pd.Series) -> pd.Series:
    """
    Fetches data from Google Cloud Storage and returns the content as a base64-encoded string.
    """
    token = None
    try:
        # Fetch the Google Cloud access token
        output = await asyncio.create_subprocess_exec(
            "gcloud", "auth", "print-access-token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await output.communicate()
        if output.returncode != 0:
            raise RuntimeError(f"Error: {stderr.decode().strip()}")
        token = stdout.decode().strip()
        if not token:
            raise ValueError("Failed to retrieve a valid access token.")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {str(e)}")

    headers = {"Authorization": f"Bearer {token}"}
    url = row["attributes.input.audio.url"]
    if url.startswith("gs://"):
        url = url.replace("gs://", "https://storage.googleapis.com/")

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            content = await response.read()

    row["audio"] = base64.b64encode(content).decode("utf-8")
    return row

```

If your data is already base64-encoded, you can skip that step.

### **Performing the Evaluation**

To run an evaluation, use the `llm_classify` function.

```python
from phoenix.evals.classify import llm_classify

# Run the evaluation
results = llm_classify(
    model=model,
    data=df,
    data_processor=async_fetch_gcloud_data,  # Optional, for private links
    template=EMOTION_PROMPT_TEMPLATE,
    rails=EMOTION_AUDIO_RAILS,
    provide_explanation=True,  # Enable explanations
)

```
