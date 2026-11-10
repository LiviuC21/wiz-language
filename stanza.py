# Fals modul stanza pentru a păcăli Argos Translate și a evita crash-ul PyTorch

def download(*args, **kwargs):
    pass  # Nu descărca nimic

class Pipeline:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, text):
        class MockSentence:
            def __init__(self, txt):
                self.text = txt

        class MockDoc:
            def __init__(self, txt):
                # Desparte simplist la punct în loc să folosească AI greoi
                parts = [p.strip() + "." for p in txt.split(".") if p.strip()]
                if not parts:
                    parts = [txt]
                self.sentences = [MockSentence(p) for p in parts]
                
        return MockDoc(text)
