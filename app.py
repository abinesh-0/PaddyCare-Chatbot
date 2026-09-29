import json
import time

import streamlit as st
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

from prompts import SYSTEM_PROMPT, WELCOME_MESSAGE_TEMPLATE


# ============================================================
# CONFIGURATION
# ============================================================

MODEL_NAME = "gemini-3.5-flash-lite"

st.set_page_config(
    page_title="PaddyCare Vision AI",
    page_icon="🌾",
    layout="centered",
)

GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]

# Kept from MacroSnap for future messaging integration.
# These are intentionally not used in the current PaddyCare UI.
TWILIO_ACCOUNT_SID = st.secrets.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = st.secrets.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = st.secrets.get("TWILIO_WHATSAPP_FROM")
TWILIO_CONTENT_SID = st.secrets.get("TWILIO_CONTENT_SID")


# ============================================================
# RELIABILITY SETTINGS
# ============================================================

RETRY_DELAYS_SECONDS = (1, 2, 4)

# Maximum amount of text history sent to Gemini for a follow-up.
MAX_CONTEXT_CHARS = 12000

# Maximum number of previous images retained in the active session.
MAX_STORED_IMAGES = 3

FRIENDLY_ERROR_MESSAGE = (
    "மன்னிக்கவும். இப்போது AI service-ஐ தொடர்பு கொள்ள முடியவில்லை. "
    "சிறிது நேரம் கழித்து உங்கள் கேள்வியை மீண்டும் கேளுங்கள்."
)


# ============================================================
# GEMINI CLIENT
# ============================================================

@st.cache_resource
def get_gemini_client():
    return genai.Client(api_key=GEMINI_API_KEY)


@st.cache_resource
def get_twilio_client():
    """
    Kept for future messaging integration.
    PaddyCare currently does not use Twilio in the main UI.
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return None

    return TwilioClient(
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
    )


gemini_client = get_gemini_client()
twilio_client = get_twilio_client()


# ============================================================
# SESSION STATE
# ============================================================

def initialize_session():
    defaults = {
        "messages": [],
        "photo_contexts": [],
        "farmer_profile": {},
        "onboarded": False,
        "name": "",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


initialize_session()


# ============================================================
# UI HELPERS
# ============================================================

def render_message(message):
    """
    Render a stored chat message.
    """

    with st.chat_message(message["role"]):

        if message["kind"] == "text":
            st.write(message["content"])

        elif message["kind"] == "image":
            st.image(
                message["content"],
                use_container_width=True,
            )


def add_message(role, kind, content):
    """
    Store and immediately render a message.
    """

    message = {
        "role": role,
        "kind": kind,
        "content": content,
    }

    st.session_state.messages.append(message)
    render_message(message)


# ============================================================
# ERROR HANDLING / RETRIES
# ============================================================

def _is_transient_error(error):
    """
    Detect temporary API/network failures that are safe to retry.
    """

    text = str(error).lower()

    markers = (
        "500",
        "502",
        "503",
        "504",
        "internal error",
        "unavailable",
        "overloaded",
        "temporarily",
        "timeout",
        "timed out",
        "try again",
        "please retry",
        "deadline exceeded",
        "resource exhausted",
        "connection reset",
        "connection aborted",
    )

    return any(marker in text for marker in markers)


def _send_with_retry(contents):
    """
    Send a Gemini request with bounded exponential backoff.
    """

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
    )

    last_error = None

    for attempt, delay in enumerate((0,) + RETRY_DELAYS_SECONDS):

        if attempt > 0:
            time.sleep(delay)

        try:
            response = gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=config,
            )

            if response and response.text:
                return response.text.strip()

            last_error = RuntimeError("Gemini returned an empty response.")

        except Exception as error:
            last_error = error

            if not _is_transient_error(error):
                break

    print(f"PaddyCare Gemini request failed: {last_error!r}")

    return None


# ============================================================
# CONVERSATION CONTEXT
# ============================================================

def _conversation_context():
    """
    Build a compact but useful representation of the conversation.

    This allows follow-up questions to use:
    - previous farmer messages
    - previous AI observations
    - crop age
    - acreage
    - water information
    - fertilizer information
    - previous questions/answers
    """

    lines = []

    messages = st.session_state.get("messages", [])

    for message in messages:

        role = message.get("role")
        kind = message.get("kind")
        content = message.get("content")

        if role == "user":
            speaker = "Farmer"
        else:
            speaker = "PaddyCare"

        if kind == "image":
            lines.append(
                f"{speaker}: [Agricultural photo was uploaded and analyzed]"
            )

        elif kind == "text":
            text = (content or "").strip()

            if text:
                lines.append(
                    f"{speaker}: {text}"
                )

    context = "\n".join(lines).strip()

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]

    return context


# ============================================================
# IMAGE CONTEXT
# ============================================================

def _get_recent_photo_contexts():
    """
    Return the most recent agricultural images.

    Keeping a small number of recent images prevents the request from
    becoming unnecessarily large while preserving useful visual context.
    """

    return st.session_state.get("photo_contexts", [])[-MAX_STORED_IMAGES:]


def _build_request(parts):
    """
    Build a Gemini request using:
    - previous conversation
    - recent agricultural photos
    - current question/photo
    """

    contents = []

    history = _conversation_context()

    if history:
        contents.append(
            "CONVERSATION CONTEXT:\n"
            + history
        )

    recent_photos = _get_recent_photo_contexts()

    for photo in recent_photos:
        contents.append(
            types.Part.from_bytes(
                data=photo["bytes"],
                mime_type=photo["mime_type"],
            )
        )

    contents.append(
        """
IMPORTANT FOLLOW-UP CONTEXT RULE:

Use the conversation context and agricultural photos together.

The farmer may ask a follow-up question without re-uploading the image.

Do not treat the latest message as an isolated question.

Use previously provided:
- crop age
- acreage
- water condition
- fertilizer information
- previous symptoms
- previous observations
- previous recommendations
- farmer's earlier questions

Only ask the farmer to repeat information if it is genuinely unavailable.
"""
    )

    contents.extend(parts)

    return contents


# ============================================================
# GEMINI QUESTION HANDLER
# ============================================================

def ask_gemini(parts):
    """
    Main AI request function.

    Supports:
    - photo-only requests
    - text-only questions
    - photo + question
    - follow-up questions using previous context
    """

    if isinstance(parts, str):
        parts = [parts]

    contents = _build_request(parts)

    answer = _send_with_retry(contents)

    if answer:
        return answer

    return FRIENDLY_ERROR_MESSAGE


# ============================================================
# TWILIO / FUTURE MESSAGING
# ============================================================

def clean_message_text(text):
    if not text:
        return "No PaddyCare summary available."

    text = " ".join(text.split())

    return text[:1500] + "..." if len(text) > 1500 else text


def send_whatsapp(to_number, user_name, summary):
    """
    Kept from MacroSnap for future use.

    PaddyCare does not expose this in the current UI.
    """

    if not twilio_client:
        return False, "Twilio is not configured."

    if not TWILIO_WHATSAPP_FROM or not TWILIO_CONTENT_SID:
        return False, "Twilio WhatsApp settings are missing."

    try:
        content_variables = json.dumps(
            {
                "1": user_name,
                "2": clean_message_text(summary),
            },
            ensure_ascii=False,
        )

        message = twilio_client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_FROM}",
            to=f"whatsapp:{to_number}",
            content_sid=TWILIO_CONTENT_SID,
            content_variables=content_variables,
        )

        return True, message.sid

    except Exception as error:
        return False, str(error)


# ============================================================
# ONBOARDING
# ============================================================

if not st.session_state.onboarded:

    st.title("🌾 PaddyCare Vision AI")

    st.caption(
        "Upload a paddy leaf or field photo and get simple, "
        "farmer-friendly agricultural guidance."
    )

    with st.form("farmer_onboarding_form"):

        name = st.text_input(
            "Farmer name",
            placeholder="Your name",
        )

        submitted = st.form_submit_button(
            "Start PaddyCare 🚜",
            use_container_width=True,
        )

    if submitted:

        if not name.strip():

            st.warning(
                "Please enter your name."
            )

        else:

            st.session_state.name = name.strip()
            st.session_state.messages = []
            st.session_state.photo_contexts = []
            st.session_state.farmer_profile = {}
            st.session_state.onboarded = True

            st.rerun()

    st.stop()


# ============================================================
# MAIN HEADER
# ============================================================

header_col, reset_col = st.columns(
    [5, 2],
    vertical_alignment="center",
)

with header_col:

    st.title("🌾 PaddyCare Vision AI")

with reset_col:

    if st.button(
        "🔄 New Session",
        use_container_width=True,
    ):

        st.session_state.messages = []
        st.session_state.photo_contexts = []
        st.session_state.farmer_profile = []

        st.rerun()


st.caption(
    f"Farmer: {st.session_state.name}"
)


# ============================================================
# INITIAL WELCOME MESSAGE
# ============================================================

if not st.session_state.messages:

    welcome_message = WELCOME_MESSAGE_TEMPLATE.format(
        name=st.session_state.name
    )

    add_message(
        "assistant",
        "text",
        welcome_message,
    )

else:

    for message in st.session_state.messages:
        render_message(message)


# ============================================================
# CHAT INPUT
# ============================================================

user_input = st.chat_input(
    "Upload a leaf/field photo or ask your question...",
    accept_file=True,
    file_type=[
        "jpg",
        "jpeg",
        "png",
        "webp",
    ],
)


# ============================================================
# USER MESSAGE PROCESSING
# ============================================================

if user_input:

    photo = (
        user_input.files[0]
        if user_input.files
        else None
    )

    text = (
        user_input.text.strip()
        if user_input.text
        else ""
    )

    current_parts = []

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if photo is not None:

        photo_bytes = photo.getvalue()
        mime_type = photo.type or "image/jpeg"

        # Store image for future follow-up questions.
        st.session_state.photo_contexts.append(
            {
                "bytes": photo_bytes,
                "mime_type": mime_type,
            }
        )

        # Keep only the most recent images.
        if len(st.session_state.photo_contexts) > MAX_STORED_IMAGES:
            st.session_state.photo_contexts = (
                st.session_state.photo_contexts[-MAX_STORED_IMAGES:]
            )

        # Display uploaded photo in chat.
        add_message(
            "user",
            "image",
            photo_bytes,
        )

        # Current request gets the current image explicitly.
        current_parts.append(
            types.Part.from_bytes(
                data=photo_bytes,
                mime_type=mime_type,
            )
        )


    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    if text:

        add_message(
            "user",
            "text",
            text,
        )

        current_parts.append(text)


    # --------------------------------------------------------
    # PHOTO ONLY
    # --------------------------------------------------------

    elif photo is not None:

        current_parts.append(
            """
Analyze this agricultural image carefully.

Determine whether it is mainly:
- a paddy leaf/plant,
- a paddy field,
- soil/land,
- or another agricultural image.

Describe what is visibly present.

Identify visible symptoms or field conditions.

Give a preliminary assessment of possible problems without pretending
that an image alone can provide a guaranteed diagnosis.

Explain practical next steps in simple farmer-friendly language.

Ask only the most important follow-up questions needed for a better
assessment, such as crop age, acreage, water condition, recent fertilizer
use, or when the problem started.

If the image is unclear, explain what additional photo would help.
"""
        )


    # --------------------------------------------------------
    # EMPTY INPUT SAFETY
    # --------------------------------------------------------

    if not current_parts:

        st.warning(
            "Please upload a photo or type a question."
        )

    else:

        with st.spinner(
            "🌾 PaddyCare is analyzing..."
        ):

            answer = ask_gemini(
                current_parts
            )

        add_message(
            "assistant",
            "text",
            answer,
        )