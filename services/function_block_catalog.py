"""Framework block catalog.

Blocks are the primitives BotStudio ships; they are never learned.
Detection method is a parameter, not a block type, so shape or YOLO
detection can be added later without invalidating existing functions.
"""

CATEGORY_ACTION = "action"
CATEGORY_LOGIC = "logic"
CATEGORY_CONTROL = "control"

CATEGORIES = (
    CATEGORY_ACTION,
    CATEGORY_LOGIC,
    CATEGORY_CONTROL,
)

SWIPE_DIRECTIONS = (
    "up",
    "down",
    "left",
    "right",
)

OCR_EXPECT_TYPES = (
    "text",
    "number",
    "alphanumeric",
    "time",
    "counter",
)

DETECTOR_TEMPLATE = "template"
DETECTOR_COLOR = "color"
DETECTOR_SHAPE = "shape"
DETECTOR_YOLO = "yolo"

# only template matching has a service today, the rest are reserved
AVAILABLE_DETECTORS = (
    DETECTOR_TEMPLATE,
    DETECTOR_COLOR,
    DETECTOR_SHAPE,
)

PLANNED_DETECTORS = (
    DETECTOR_COLOR,
    DETECTOR_SHAPE,
    DETECTOR_YOLO,
)

BLOCK_FIND_ELEMENT = "find_element"
BLOCK_COORDINATE_ANCHOR = "coordinate_anchor"
BLOCK_FIND_COLOR_TRIGGER = "find_color_trigger"
BLOCK_FIND_SHAPE = "find_shape"
BLOCK_FIND_AND_TAP_ELEMENT = "find_and_tap_element"
BLOCK_TAP_ELEMENT = "tap_element"
BLOCK_WAIT_FOR_ELEMENT_AND_TAP = "wait_for_element_and_tap"
BLOCK_TAP_POINT = "tap_point"
BLOCK_ANDROID_BACK = "android_back"
BLOCK_PRESS_ENTER = "press_enter"
BLOCK_CLEAR_FIELD = "clear_field"
BLOCK_TYPE_TEXT = "type_text"
BLOCK_SWIPE = "swipe"
BLOCK_SWIPE_SEARCH = "swipe_search"
# pause after the last swipe so a flung list has stopped scrolling/bouncing
DEFAULT_SWIPE_SETTLE_MS = 800
BLOCK_SWIPE_SEARCH_TAP = "swipe_search_tap"
BLOCK_OCR_READ = "ocr_read"
BLOCK_SEARCH_OCR = "search_ocr"
BLOCK_SEARCH_OCR_TAP = "search_ocr_tap"
BLOCK_WAIT = "wait"
BLOCK_WAIT_FOR_ELEMENT = "wait_for_element"
BLOCK_CALL_FUNCTION = "call_function"
BLOCK_CONDITION = "condition"
BLOCK_COMPARE = "compare"
BLOCK_GATE = "gate"
BLOCK_MATH = "math"
BLOCK_REPEAT = "repeat"
BLOCK_STOP_EVENT = "stop_event"
BLOCK_SET_EVENT_DUE = "set_event_due"
BLOCK_RESCHEDULE_EVENT = "reschedule_event"
BLOCK_SPLITTER = "splitter"
BLOCK_TIMER = "timer"
BLOCK_DUE_DATE_REMAINING = "due_date_remaining"
BLOCK_VARIABLE = "variable"
BLOCK_CONFIGURE_FIELD = "configure_field"
BLOCK_NEGATE_OUTCOME = "negate_outcome"
BLOCK_SUCCESS = "success_terminal"
BLOCK_FAILURE = "failure_terminal"
BLOCK_DURATION_PARSE = "duration_parse"

MATH_OPERATORS = ("+", "-", "*", "/")

OUTCOME_FOUND = "found"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_TRUE = "true"
OUTCOME_FALSE = "false"
OUTCOME_DONE = "done"
OUTCOME_BODY = "body"
OUTCOME_OUT1 = "out_1"
OUTCOME_OUT2 = "out_2"
OUTCOME_OUT3 = "out_3"
OUTCOME_OUT4 = "out_4"
OUTCOME_ELAPSED = "elapsed"
OUTCOME_PENDING = "pending"
OUTCOME_SUCCESS = "success"
OUTCOME_COORDINATES = "coordinates"

GATE_OPERATORS = (
    "and",
    "or",
    "not",
    "nand",
    "nor",
    "xor",
)

COMPARE_OPERATORS = (
    "==",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
)


BLOCK_CATALOG = {

    BLOCK_FIND_ELEMENT: {
        "category": CATEGORY_ACTION,
        "label": "Find element",
        "description": "Find a learned library element in its search area.",
        "params": {
            "library_entry_id": "",
            "detector": DETECTOR_TEMPLATE,
                "threshold": 98,
            "region": None,
            "attempts": 1,
            "delay_ms": 300,
        },
        "outcomes": [
            OUTCOME_FOUND,
            OUTCOME_NOT_FOUND,
            OUTCOME_COORDINATES,
        ],
        "returns": {
            "name": "match",
            "type": "region",
        },
    },

    BLOCK_COORDINATE_ANCHOR: {
        "category": CATEGORY_CONTROL,
        "label": "Coordinate anchor",
        "description": "Provide a fixed screen region for OCR or coordinate-aware blocks.",
        "params": {
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 40,
            "screen_width": 0,
            "screen_height": 0,
        },
        "outcomes": [
            OUTCOME_COORDINATES,
            OUTCOME_FAILED,
        ],
        "returns": {
            "name": "coordinates",
            "type": "region",
        },
    },

    BLOCK_FIND_COLOR_TRIGGER: {
        "category": CATEGORY_ACTION,
        "label": "Find color trigger",
        "description": "Find a learned color region and return its coordinates.",
        "params": {
            "library_entry_id": "",
            "region": None,
        },
        "outcomes": [
            OUTCOME_FOUND,
            OUTCOME_NOT_FOUND,
        ],
        "returns": {
            "name": "match",
            "type": "region",
        },
    },

    BLOCK_FIND_SHAPE: {
        "category": CATEGORY_ACTION,
        "label": "Find shape",
        "description": "Find a learned shape using its detection profile.",
        "params": {
            "library_entry_id": "",
            "threshold": 82,
            "region": None,
        },
        "outcomes": [
            OUTCOME_FOUND,
            OUTCOME_NOT_FOUND,
        ],
        "returns": {
            "name": "match",
            "type": "region",
        },
    },

    BLOCK_FIND_AND_TAP_ELEMENT: {
        "category": CATEGORY_ACTION,
        "label": "Find and tap element",
        "description": "Find a library element and tap it immediately.",
        "params": {
            "library_entry_id": "",
            "detector": DETECTOR_TEMPLATE,
                "threshold": 98,
            "region": None,
            "attempts": 1,
            "delay_ms": 300,
            "offset_x": 0,
            "offset_y": 0,
            "tap_delay_ms": 500,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_TAP_ELEMENT: {
        "category": CATEGORY_ACTION,
        "label": "Tap element",
        "description": "Find a library element and tap it.",
        "params": {
            "library_entry_id": "",
            "detector": DETECTOR_TEMPLATE,
                "threshold": 98,
            "region": None,
            "offset_x": 0,
            "offset_y": 0,
            "tap_delay_ms": 500,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_WAIT_FOR_ELEMENT_AND_TAP: {
        "category": CATEGORY_ACTION,
        "label": "Wait for element and tap",
        "description": "Wait until a learned element appears and then tap it.",
        "params": {
            "library_entry_id": "",
            "detector": DETECTOR_TEMPLATE,
                "threshold": 98,
            "region": None,
            "timeout_ms": 10000,
            "poll_ms": 500,
            "offset_x": 0,
            "offset_y": 0,
            "tap_delay_ms": 500,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_TAP_POINT: {
        "category": CATEGORY_ACTION,
        "label": "Tap point",
        "description": "Tap a fixed coordinate.",
        "params": {
            "x": 0,
            "y": 0,
            "tap_delay_ms": 500,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_ANDROID_BACK: {
        "category": CATEGORY_ACTION,
        "label": "Android back",
        "description": "Send the Android back key to the device.",
        "params": {},
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_CLEAR_FIELD: {
        "category": CATEGORY_ACTION,
        "label": "Clear field",
        "description": "Tap a field and delete its content via backspace.",
        "params": {
            "x": 0,
            "y": 0,
            "count": 32,
            "tap_delay_ms": 500,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_PRESS_ENTER: {
        "category": CATEGORY_ACTION,
        "label": "Press enter",
        "description": "Send the Enter key to the device.",
        "params": {
            "delay_ms": 500,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_TYPE_TEXT: {
        "category": CATEGORY_ACTION,
        "label": "Type text",
        "description": "Tap a field and type a fixed value or a variable into it.",
        "params": {
            "x": 0,
            "y": 0,
            "text": "",
            "tap_delay_ms": 500,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_SWIPE: {
        "category": CATEGORY_ACTION,
        "label": "Swipe",
        "description": "Perform a swipe gesture.",
        "params": {
            "direction": "up",
            "start_x": 0,
            "start_y": 0,
            "max_swipes": 1,
            "duration_ms": 300,
            "between_swipes_ms": 200,
            "settle_ms": DEFAULT_SWIPE_SETTLE_MS,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_SWIPE_SEARCH: {
        "category": CATEGORY_ACTION,
        "label": "Swipe search",
        "description": "Swipe through an area until an element is found.",
        "params": {
            "library_entry_id": "",
            "detector": DETECTOR_TEMPLATE,
                "threshold": 98,
            "region": None,
            "direction": "up",
            "start_x": 0,
            "start_y": 0,
            "max_swipes": 5,
            "duration_ms": 300,
            "between_swipes_ms": 200,
        },
        "outcomes": [
            OUTCOME_FOUND,
            OUTCOME_NOT_FOUND,
            OUTCOME_COORDINATES,
        ],
        "returns": {
            "name": "match",
            "type": "region",
        },
    },

    BLOCK_SWIPE_SEARCH_TAP: {
        "category": CATEGORY_ACTION,
        "label": "Swipe Search + Tap",
        "description": "Swipe until a library target is found, then tap its center plus offset.",
        "params": {
            "library_entry_id": "",
            "detector": DETECTOR_TEMPLATE,
            "threshold": 98,
            "region": None,
            "direction": "up",
            "start_x": 0,
            "start_y": 0,
            "max_swipes": 5,
            "duration_ms": 300,
            "between_swipes_ms": 200,
            "offset_x": 0,
            "offset_y": 0,
            "tap_delay_ms": 500,
        },
        "outcomes": [OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_FAILED],
        "returns": {"name": "match", "type": "region"},
    },

    BLOCK_OCR_READ: {
        "category": CATEGORY_ACTION,
        "label": "Read OCR",
        "description": "Read text or numbers from an area.",
        "params": {
            "region": None,
            "expect": "text",
            "delay_before_read_ms": 200,
            "language": "",
            "library_entry_id": "",
            "counter_part": "left",
            "default_value": "",
            "offset_x": 0,
            "offset_y": 0,
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {
            "name": "text",
            "type": "string",
        },
    },

    BLOCK_SEARCH_OCR: {
        "category": CATEGORY_ACTION,
        "label": "Search OCR",
        "description": "Find text on the screen and return its coordinates.",
        "params": {
            "query": "",
            "region": None,
            "language": "",
        },
        "outcomes": [
            OUTCOME_FOUND,
            OUTCOME_NOT_FOUND,
            OUTCOME_COORDINATES,
        ],
        "returns": {
            "name": "match",
            "type": "region",
        },
    },

    BLOCK_SEARCH_OCR_TAP: {
        "category": CATEGORY_ACTION,
        "label": "Search OCR + Tap",
        "description": "Find matching text and tap its center plus offset.",
        "params": {
            "query": "",
            "region": None,
            "language": "",
            "offset_x": 0,
            "offset_y": 0,
            "tap_delay_ms": 500,
        },
        "outcomes": [OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_FAILED],
        "returns": {"name": "match", "type": "region"},
    },

    BLOCK_WAIT: {
        "category": CATEGORY_CONTROL,
        "label": "Wait",
        "description": "Wait for a fixed duration.",
        "params": {
            "duration_ms": 1000,
        },
        "outcomes": [
            OUTCOME_DONE,
        ],
        "returns": {},
    },

    BLOCK_WAIT_FOR_ELEMENT: {
        "category": CATEGORY_CONTROL,
        "label": "Wait for element",
        "description": "Wait until an element appears or the timeout expires.",
        "params": {
            "library_entry_id": "",
            "detector": DETECTOR_TEMPLATE,
                "threshold": 98,
            "region": None,
            "timeout_ms": 10000,
            "poll_ms": 500,
        },
        "outcomes": [
            OUTCOME_FOUND,
            OUTCOME_NOT_FOUND,
            OUTCOME_COORDINATES,
        ],
        "returns": {
            "name": "match",
            "type": "region",
        },
    },

    BLOCK_CALL_FUNCTION: {
        "category": CATEGORY_CONTROL,
        "label": "Call function",
        "description": "Call a sealed helper function.",
        "params": {
            "function_id": "",
            "inputs": {},
        },
        "outcomes": [
            OUTCOME_SUCCESS,
            OUTCOME_FAILED,
        ],
        "returns": {
            "name": "result",
            "type": "any",
        },
    },

    BLOCK_CONDITION: {
        "category": CATEGORY_LOGIC,
        "label": "Condition",
        "description": "Branch based on a condition.",
        "params": {
            "expression": "",
        },
        "outcomes": [
            OUTCOME_TRUE,
            OUTCOME_FALSE,
        ],
        "returns": {},
    },

    BLOCK_COMPARE: {
        "category": CATEGORY_LOGIC,
        "label": "Compare",
        "description": "Compare a value such as an OCR cooldown.",
        "params": {
            "left": "",
            "operator": "==",
            "right": "",
        },
        "outcomes": [
            OUTCOME_TRUE,
            OUTCOME_FALSE,
        ],
        "returns": {},
    },
    BLOCK_MATH: {
        "category": CATEGORY_LOGIC,
        "label": "Math",
        "description": "Compute a numeric value from two inputs or variables.",
        "params": {
            "left": "",
            "operator": "+",
            "right": "",
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {
            "name": "result",
            "type": "number",
        },
    },
    BLOCK_DURATION_PARSE: {
        "category": CATEGORY_LOGIC,
        "label": "Duration parse",
        "description": "Parse a duration text (e.g. OCR cooldown) into seconds.",
        "params": {
            "input": "",
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {
            "name": "seconds",
            "type": "number",
        },
    },
    BLOCK_DUE_DATE_REMAINING: {
        "category": CATEGORY_LOGIC,
        "label": "Due date remaining",
        "description": "Calculate remaining seconds until a due date.",
        "params": {
            "input": "",
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {
            "name": "seconds",
            "type": "number",
        },
    },

    BLOCK_GATE: {
        "category": CATEGORY_LOGIC,
        "label": "Logic gate",
        "description": "Combine multiple boolean values.",
        "params": {
            "operator": "and",
            "operands": [],
        },
        "outcomes": [
            OUTCOME_TRUE,
            OUTCOME_FALSE,
        ],
        "returns": {},
    },

    BLOCK_REPEAT: {
        "category": CATEGORY_CONTROL,
        "label": "Repeat",
        "description": "Repeat a branch with iteration and time limits.",
        "params": {
            "max_iterations": 10,
            "timeout_ms": 30000,
            "until_variable": "",
            "until_failed": False,
        },
        "outcomes": [
            OUTCOME_BODY,
            OUTCOME_DONE,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_STOP_EVENT: {
        "category": CATEGORY_CONTROL,
        "label": "Stop event",
        "description": "Stop this event after a configured number of arrivals and reschedule it.",
        "params": {
            "stop_after": 1,
            "delay_seconds": 0,
            "delay_variable": "",
        },
        "outcomes": [
            OUTCOME_OK,
        ],
        "returns": {
            "name": "reschedule_seconds",
            "type": "number",
        },
    },

    BLOCK_SET_EVENT_DUE: {
        "category": CATEGORY_CONTROL,
        "label": "Set event due",
        "description": "Mark another event as due now so the scheduler runs it next.",
        "params": {
            "event_id": "",
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_RESCHEDULE_EVENT: {
        "category": CATEGORY_CONTROL,
        "label": "Reschedule event",
        "description": "Schedule an event in this account (including the running one) after a delay without stopping this event.",
        "params": {
            "event_id": "",
            "delay_seconds": 0,
            "delay_variable": "",
        },
        "outcomes": [OUTCOME_OK, OUTCOME_FAILED],
        "returns": {},
    },

    BLOCK_SPLITTER: {
        "category": CATEGORY_CONTROL,
        "label": "Splitter",
        "description": "Split execution into multiple parallel branches.",
        "params": {
            "outputs": 2,
        },
        "outcomes": [
            OUTCOME_OUT1,
            OUTCOME_OUT2,
        ],
        "returns": {},
    },

    BLOCK_TIMER: {
        "category": CATEGORY_LOGIC,
        "label": "Timer",
        "description": "Check whether a cooldown has elapsed.",
        "params": {
            "variable": "",
            "margin_seconds": 0,
        },
        "outcomes": [
            OUTCOME_ELAPSED,
            OUTCOME_PENDING,
        ],
        "returns": {},
    },

    BLOCK_VARIABLE: {
        "category": CATEGORY_CONTROL,
        "label": "Variable",
        "description": "Read or write a local or global runtime variable.",
        "params": {
            "mode": "read",
            "variable": "",
            "value": "",
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {
            "name": "value",
            "type": "any",
        },
    },

    BLOCK_CONFIGURE_FIELD: {
        "category": CATEGORY_CONTROL,
        "label": "Configure field",
        "description": "Declare a user-configurable Event or Task field.",
        "params": {
            "variable_name": "",
            "field_target": "task",
            "variable_kind": "config",
            "value_type": "text",
            "description": "",
            "initial_value": "",
            "required": False,
            "writable_by_functions": False,
            "options_source": "none",
            "fixed_options": "[]",
            "options_variable": "",
        },
        "outcomes": [
            OUTCOME_OK,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_NEGATE_OUTCOME: {
        "category": CATEGORY_LOGIC,
        "label": "Negate outcome",
        "description": "Forward success as failure and failure as success.",
        "params": {
            "outcome": "success",
        },
        "outcomes": [
            OUTCOME_SUCCESS,
            OUTCOME_FAILED,
        ],
        "returns": {},
    },

    BLOCK_SUCCESS: {
        "category": CATEGORY_CONTROL,
        "label": "Success",
        "description": "Finish the current path successfully without an action.",
        "params": {},
        "outcomes": [
            OUTCOME_SUCCESS,
        ],
        "returns": {},
    },

    BLOCK_FAILURE: {
        "category": CATEGORY_CONTROL,
        "label": "Failure",
        "description": "Finish the current path as failed without an action.",
        "params": {},
        "outcomes": [
            OUTCOME_FAILED,
        ],
        "returns": {},
    },
}


BLOCK_HOW_TO = {
    BLOCK_FIND_ELEMENT: "1. Select the learned Library entry and verify its Search Boundary.\n2. Start with the default threshold; raise it only when false positives matter.\n3. Connect found to the success path, not_found to a bounded recovery path, and pass the match through coordinates to OCR or other coordinate-aware blocks.\n4. Do not use a fixed coordinate as a silent fallback.",
    BLOCK_COORDINATE_ANCHOR: "1. Capture a current Live Screenshot.\n2. Mark the smallest stable region containing the text or control.\n3. Connect coordinates to OCR or another coordinate-aware block.\n4. Recheck the anchor after resolution or orientation changes.",
    BLOCK_FIND_COLOR_TRIGGER: "1. Select a learned Color Trigger entry.\n2. Check its Search Boundary, tolerance and minimum area.\n3. Use found/not_found explicitly; pass coordinates only to the intended next block.\n4. Avoid treating a color match as proof that the complete screen is correct.",
    BLOCK_FIND_SHAPE: "1. Select a learned Shape entry.\n2. Set threshold and keep the Search Boundary tight.\n3. Route found and not_found separately.\n4. Verify the shape on more than one screenshot before relying on it.",
    BLOCK_FIND_AND_TAP_ELEMENT: "1. Select the target Library entry.\n2. Use offsets only when the visual anchor differs from the tap target.\n3. Continue from ok only after the detection and tap succeeded.\n4. Handle failed explicitly so the next action is not triggered accidentally.",
    BLOCK_TAP_ELEMENT: "1. Select the target Library entry.\n2. Prefer an incoming coordinates anchor when the screen contains repeated targets.\n3. Configure the optional tap offset in screenshot pixels.\n4. Handle failed explicitly.",
    BLOCK_WAIT_FOR_ELEMENT_AND_TAP: "1. Select the target Library entry.\n2. Set a finite timeout and polling interval.\n3. The pre-tap delay (default 500 ms) lets the screen settle before the tap.\n4. Route failed to a bounded recovery or stop path.",
    BLOCK_TAP_POINT: "1. Set fallback coordinates only for a stable, verified screen position.\n2. Prefer an incoming coordinates connection when available.\n3. Keep the action outside the GUI thread and respect cancellation.\n4. Handle failed explicitly.",
    BLOCK_ANDROID_BACK: "1. Place Back after a failed screen or navigation check.\n2. Connect ok to the next bounded step or loop iteration.\n3. Handle failed because the emulator may reject the key event.\n4. Never use Back as an unbounded recovery loop.",
    BLOCK_CLEAR_FIELD: "1. Set the field coordinates or wire an incoming coordinates connection.\n2. The block taps the field first, then sends backspace presses.\n3. Choose the delete count generously enough for the longest expected content.\n4. Handle failed explicitly.",
    BLOCK_PRESS_ENTER: "1. Use after typing into a field or to confirm a dialog.\n2. Set the delay so the screen settles before the key lands (default 500 ms).\n3. Handle failed because the emulator may reject the key event.",
    BLOCK_TYPE_TEXT: "1. Set the field coordinates or wire an incoming coordinates connection.\n2. Enter a fixed value or a $variable reference as the text.\n3. Combine with Clear field first when the field may already contain text.\n4. Handle failed explicitly.",
    BLOCK_SWIPE: "1. Choose direction, start/end behavior and gesture duration.\n2. Set a positive pause between repeated swipes so the game can process each gesture.\n3. Keep max_swipes finite.\n4. Continue from ok and handle failed.",
    BLOCK_SWIPE_SEARCH: "1. Select the stable search anchor.\n2. Set gesture duration, direction, max swipes and between-swipes pause.\n3. Route found and coordinates deliberately to the next block.\n4. Handle not_found/failed without retrying forever.",
    BLOCK_SWIPE_SEARCH_TAP: "1. Select the search anchor.\n2. Set bounded swipe options and a positive pause for repeated gestures.\n3. The tap target is the found center plus the configured X/Y offset.\n4. Handle not_found and failed separately.",
    BLOCK_OCR_READ: "1. Select a region in the Live Preview, or provide a Library entry or incoming coordinates. Drawing a region resets old OCR offsets.\n2. With Library and incoming coordinates, the taught pattern size is read at the match anchor plus offsets. Clear region restores this fallback.\n3. Choose the expected type: text, number, time, counter or alphanumeric. The default 200 ms delay before capture can be adjusted or set to 0.\n4. Optionally set a default value or $variable reference returned when nothing is readable; it is interpreted as number or text automatically to match the expected type.\n5. Store the result under a named output and handle failed; never reuse an old OCR value as current.",
    BLOCK_SEARCH_OCR: "1. Enter the exact visible text or a variable reference.\n2. Use the Live Preview to limit the search region when the screen contains unrelated text.\n3. Profile names may be split into several OCR boxes; the search combines a line before matching.\n4. Route found/coordinates and handle not_found explicitly.",
    BLOCK_SEARCH_OCR_TAP: "1. Enter the profile name or other text to find.\n2. Limit the search region when several rows contain similar names.\n3. The tap target is the complete matched text region plus the configured offset.\n4. Handle not_found and failed without an unbounded retry.",
    BLOCK_WAIT: "1. Set a finite duration in milliseconds.\n2. Waits run outside the GUI thread and observe cancellation.\n3. Keep delays short inside loops and use a timeout on the loop itself.\n4. Continue from done.",
    BLOCK_WAIT_FOR_ELEMENT: "1. Select the target Library entry.\n2. Set a finite timeout and polling interval.\n3. Continue from found only after detection succeeds; pass the match through coordinates when the next block needs the position.\n4. Route not_found deliberately.",
    BLOCK_CALL_FUNCTION: "1. Select an allowed Helper Function.\n2. Fill every declared input with a literal or variable reference.\n3. Route success and failed separately.\n4. The Helper remains a closed capsule; its internals are not edited here.",
    BLOCK_CONDITION: "1. Enter a variable or expression that resolves to a boolean value.\n2. Connect true and false to explicit paths.\n3. Avoid hidden writes or side effects in the condition.\n4. Test both branches with representative profile values.",
    BLOCK_COMPARE: "1. Set left value, operator and right value.\n2. Use variable references for live or profile data.\n3. Normalize OCR text before numeric comparisons.\n4. Connect true and false explicitly.",
    BLOCK_GATE: "1. Choose AND, OR, NOT or another supported operator.\n2. Set the operand count and connect every input deliberately.\n3. Keep each operand a boolean-producing block or variable.\n4. Handle both true and false outputs.",
    BLOCK_REPEAT: "1. Connect the body and define a finite max iteration count.\n2. Add an until variable or explicit break when the loop can finish early.\n3. Set a timeout as a second safety boundary.\n4. Route done and failed; never rely on an unbounded loop.",
    BLOCK_STOP_EVENT: "1. Connect the normal path through this block.\n2. Set the arrival count that triggers the stop.\n3. Choose fixed seconds or a seconds variable; reaching the limit automatically ends the event and schedules its next run.",
    BLOCK_SET_EVENT_DUE: "1. Select a target event in this account.\n2. The event becomes due now and runs at the next scheduler opportunity.\n3. This function continues normally; disabled, protected or running targets are rejected.",
    BLOCK_RESCHEDULE_EVENT: "1. Select a target event in this account.\n2. Choose fixed seconds or a seconds variable.\n3. The target is scheduled from now plus the delay; this function continues normally.",
    BLOCK_SPLITTER: "1. Set the output count.\n2. Connect each output deliberately.\n3. Avoid accidental parallel side effects.",
    BLOCK_TIMER: "1. Select the profile-bound timer variable.\n2. Set the allowed margin in seconds.\n3. Route elapsed to the action and pending to a bounded wait or scheduler path.\n4. Do not use stale timer values after a failed live read.",
    BLOCK_VARIABLE: "1. Choose read or write.\n2. Enter the user-defined profile variable name; local variables stay inside the Function.\n3. Use Config/Profile values as read-only unless explicitly permitted.\n4. Never persist temporary loop or control flags as profile state.",
    BLOCK_CONFIGURE_FIELD: "1. Enter a stable user-visible variable name.\n2. Choose Event or Task scope and Config, Profile or Live kind.\n3. Select the value type, default, required state and optional Collection source.\n4. Save the Function; conflicts and missing dependencies reject the save visibly.",
    BLOCK_NEGATE_OUTCOME: "1. Set the explicit outcome.\n2. Connect success and failed.\n3. Do not use as a hidden success fallback.",
    BLOCK_SUCCESS: "1. Use for an already-complete state.\n2. Connect it to the successful path.\n3. Do not hide real failures.",
    BLOCK_FAILURE: "1. Use for a deliberate terminal failure.\n2. Connect only genuine failure paths.\n3. Inspect the trace afterward.",
    BLOCK_DURATION_PARSE: "1. Provide OCR duration text.\n2. Connect ok to the next action.\n3. Use the output for scheduling.",
    BLOCK_DUE_DATE_REMAINING: "1. Provide an ISO date/time or a $variable reference.\n2. Empty or past due dates return 0 seconds.\n3. Store the output to compare remaining cooldowns or calculate a new due date.\n4. Invalid date values follow failed.",
    BLOCK_MATH: "1. Set left and right values or $variables.\n2. Choose +, -, * or /.\n3. The result is stored under the named output for the next block.\n4. Division by zero and non-numeric input return failed.",
}

for _block_type, _how_to in BLOCK_HOW_TO.items():
    if _block_type in BLOCK_CATALOG:
        BLOCK_CATALOG[_block_type]["how_to"] = _how_to


def collect_field_declarations(function) -> list[dict]:
    """Return Configure Field contracts stored in one Function graph."""

    declarations = []
    for block in function.walk_blocks():
        if block.block_type != BLOCK_CONFIGURE_FIELD:
            continue
        declaration = dict(block.params)
        declaration["function_id"] = function.function_id
        declaration["function_name"] = function.name or function.function_id
        declarations.append(declaration)
    return declarations


class FunctionBlockCatalog:

    def list_categories(self) -> list:
        return list(CATEGORIES)

    def list_types(
        self,
        category: str | None = None,
    ) -> list:

        return [
            block_type
            for block_type, spec in BLOCK_CATALOG.items()
            if category is None
            or spec["category"] == category
        ]

    def get(
        self,
        block_type: str,
    ) -> dict | None:

        return BLOCK_CATALOG.get(
            block_type
        )

    def default_params(
        self,
        block_type: str,
    ) -> dict:

        spec = self.get(
            block_type
        )

        if not spec:
            return {}

        return dict(
            spec["params"]
        )

    def outcomes(
        self,
        block_type: str,
    ) -> list:

        spec = self.get(
            block_type
        )

        if not spec:
            return []

        return list(
            spec["outcomes"]
        )

    def returns(
        self,
        block_type: str,
    ) -> dict:

        spec = self.get(
            block_type
        )

        if not spec:
            return {}

        return dict(
            spec.get("returns", {})
        )

    def label(
        self,
        block_type: str,
    ) -> str:

        spec = self.get(
            block_type
        )

        if not spec:
            return ""

        return spec.get("label", "")

    def description(
        self,
        block_type: str,
    ) -> str:

        spec = self.get(
            block_type
        )

        if not spec:
            return ""

        return spec.get("description", "")

    def category(
        self,
        block_type: str,
    ) -> str:

        spec = self.get(
            block_type
        )

        if not spec:
            return ""

        return spec.get("category", "")

    def is_detector_available(
        self,
        detector: str,
    ) -> bool:

        return detector in AVAILABLE_DETECTORS
