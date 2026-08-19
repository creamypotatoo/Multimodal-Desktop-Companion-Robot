from intelligence.multimodal import fuse

# Simulate a few different perception scenarios
snapshot1 = fuse(person_name="sera", emotion="happy", heard_text="hello")
print(snapshot1.describe())

snapshot2 = fuse(person_name=None, emotion="neutral", heard_text=None)
print(snapshot2.describe())

snapshot3 = fuse()
print(snapshot3.describe())