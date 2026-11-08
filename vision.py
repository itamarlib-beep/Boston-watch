"""Optional on-device breed triage. ImageNet label scores are NOT calibrated breed probabilities."""
from __future__ import annotations
from io import BytesIO
from urllib.parse import urlparse
from PIL import Image

class BreedTriage:
    def __init__(self):
        import torch
        from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small
        self.torch = torch
        self.weights = MobileNet_V3_Small_Weights.DEFAULT
        self.model = mobilenet_v3_small(weights=self.weights).eval()
        self.transform = self.weights.transforms()
        self.categories = self.weights.meta['categories']
        self.boston_ids = [i for i, name in enumerate(self.categories) if 'boston bull' in name.casefold()]
        if not self.boston_ids:
            raise RuntimeError('Pretrained model lacks Boston bull class')

    def score(self, image_url: str, session) -> dict:
        p = urlparse(image_url)
        if p.scheme != 'https' or not p.hostname:
            raise ValueError('Image URL must be HTTPS')
        # Only fetch images from explicit public media hosts; prevents untrusted HTML
        # from making our GitHub runner request internal network resources.
        allowed = ('yad4.s3.amazonaws.com', 'yad4.co.il', 'www.yad4.co.il')
        if p.hostname.lower() not in allowed:
            raise ValueError('Image host not allowlisted')
        resp = session.get(image_url, timeout=15, headers={'User-Agent': 'BostonAdoptionWatch/1.1'})
        resp.raise_for_status()
        if len(resp.content) > 5_000_000:
            raise ValueError('Image exceeds size limit')
        with Image.open(BytesIO(resp.content)) as original:
            img = original.convert('RGB')
            if img.width * img.height > 20_000_000:
                raise ValueError('Image pixel count too large')
            tensor = self.transform(img).unsqueeze(0)
        with self.torch.inference_mode():
            scores = self.model(tensor).softmax(1)[0]
        idx = int(scores.argmax())
        boston_score = max(float(scores[i]) for i in self.boston_ids)
        top5 = [self.categories[int(i)] for i in scores.topk(5).indices]
        return {'breed_score': round(boston_score, 4), 'top_breed': self.categories[idx], 'top5': top5,
                'model': 'MobileNetV3-Small ImageNet-1K'}
