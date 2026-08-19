import cv2
import numpy as np
import onnxruntime as ort

MODEL_PATH = "data/models/emotion-ferplus-8.onnx"

# Order matters -- this is the exact label order the model was trained on
EMOTIONS = ["neutral", "happy", "surprise", "sad",
            "angry", "disgust", "fear", "contempt"]

session = ort.InferenceSession(MODEL_PATH)
input_name = session.get_inputs()[0].name

def detect_emotion(face_crop_gray):
    """
    Takes a cropped, grayscale face image (any size).
    Returns the predicted emotion as a string.
    """
    # The model expects exactly 64x64 grayscale input
    resized = cv2.resize(face_crop_gray, (64, 64))

    # Reshape to (1, 1, 64, 64) -- batch size 1, 1 color channel, height, width
    input_tensor = resized.astype(np.float32).reshape(1, 1, 64, 64)

    outputs = session.run(None, {input_name: input_tensor})
    scores = outputs[0][0]

    predicted_index = np.argmax(scores)
    return EMOTIONS[predicted_index]