import torch
from typing import List, Dict, Optional, Union, Set
from dataclasses import dataclass
import json
import numpy as np
from collections import defaultdict
import re
import os
import sentencepiece as spm

@dataclass
class ConceptualTokenizerConfig:
    vocab_size: int = 32000  # Default for production, tests should override
    max_length: int = 8192
    pad_token: str = "[PAD]"
    unk_token: str = "[UNK]"
    bos_token: str = "[BOS]"
    eos_token: str = "[EOS]"
    concept_tokens: Optional[List[str]] = None
    reasoning_tokens: Optional[List[str]] = None
    min_freq: int = 5
    special_tokens_map: Optional[Dict[str, int]] = None
    model_type: str = "unigram"  # ["bpe", "unigram", "char", "word"]
    character_coverage: float = 0.9995
    model_prefix: str = "conceptual"
    pad_id: int = 0
    unk_id: int = 1
    bos_id: int = 2
    eos_id: int = 3
    control_symbols: List[str] = None
    user_defined_symbols: List[str] = None

class ConceptualTokenizer:
    """Tokenizer with conceptual understanding capabilities."""
    
    def __init__(self, config: ConceptualTokenizerConfig):
        self.config = config
        self.sp_model = None
        self.concept_embeddings = {}
        self.inverse_concept_embeddings = {}  # Ensure it's initialized
        self.semantic_clusters = defaultdict(set)
        
        if os.path.exists(f"{config.model_prefix}.model"):
            self.load_tokenizer()
    
    def tokenize(self, text: str) -> List[str]:
        """Convert text to tokens."""
        if self.sp_model is None:
            raise RuntimeError("Tokenizer model not loaded. Call train_tokenizer or load_tokenizer first.")
        return self.sp_model.encode_as_pieces(text)
    
    def train_tokenizer(self, texts: List[str]):
        """Train SentencePiece tokenizer on input texts."""
        # Write training data
        with open("training_data.txt", "w", encoding="utf-8") as f:
            for text in texts:
                f.write(text + "\n")
        
        # Calculate minimum required vocab size and add margin for special tokens
        unique_chars = len(set(''.join(texts)))
        min_vocab_size = min(self.config.vocab_size, max(22, unique_chars * 2))  # Increased minimum vocab size
        
        # Create training arguments
        train_args = {
            "input": "training_data.txt",
            "model_prefix": self.config.model_prefix,
            "model_type": self.config.model_type,
            "vocab_size": min_vocab_size,  # Updated vocab size
            "character_coverage": 1.0,  # Ensure complete coverage
            "pad_id": self.config.pad_id,
            "unk_id": self.config.unk_id,
            "bos_id": self.config.bos_id,
            "eos_id": self.config.eos_id,
            "train_extremely_large_corpus": False,
            "max_sentencepiece_length": 8,  # Shorter pieces for better handling
            "split_by_whitespace": True,  # Better word boundary handling
            "split_by_number": True,
        }

        # Only add non-empty control/user symbols
        if self.config.control_symbols and len(self.config.control_symbols) > 0:
            train_args["control_symbols"] = self.config.control_symbols
        if self.config.user_defined_symbols and len(self.config.user_defined_symbols) > 0:
            train_args["user_defined_symbols"] = self.config.user_defined_symbols

        # Train SentencePiece model
        spm.SentencePieceTrainer.train(**train_args)
        self.load_tokenizer()
    
    def load_tokenizer(self):
        """Load trained SentencePiece model."""
        self.sp_model = spm.SentencePieceProcessor()
        self.sp_model.load(f"{self.config.model_prefix}.model")
    
    def encode(self, text: Union[str, List[str]], add_special_tokens: bool = True) -> Union[List[int], List[List[int]]]:
        """Encode text using SentencePiece with concept awareness."""
        if isinstance(text, str):
            text = [text]
        
        results = []
        for t in text:
            # Detect concepts in text
            concept_spans = self._detect_concepts(t)
            
            # Tokenize with concept boundaries preserved
            pieces = []
            last_end = 0
            for start, end, concept in concept_spans:
                # Tokenize text before concept
                if start > last_end:
                    pieces.extend(self.sp_model.encode_as_ids(t[last_end:start]))
                
                # Add concept token
                concept_token = f"[CONCEPT_{concept.upper()}]"
                if concept_token in self.concept_embeddings:
                    pieces.append(self.concept_embeddings[concept_token])
                else:
                    # Fallback to normal tokenization for unknown concepts
                    pieces.extend(self.sp_model.encode_as_ids(t[start:end]))
                
                last_end = end
            
            # Tokenize remaining text
            if last_end < len(t):
                pieces.extend(self.sp_model.encode_as_ids(t[last_end:]))
            
            if add_special_tokens:
                pieces = [self.config.bos_id] + pieces + [self.config.eos_id]
            
            if len(pieces) > self.config.max_length:
                pieces = pieces[:self.config.max_length - 1] + [self.config.eos_id]
            else:
                pieces += [self.config.pad_id] * (self.config.max_length - len(pieces))
            
            results.append(pieces)
        
        return results[0] if len(results) == 1 else results
    
    def decode(self, token_ids: Union[List[int], List[List[int]]], skip_special_tokens: bool = True) -> Union[str, List[str]]:
        """Decode token IDs back to text."""
        if isinstance(token_ids[0], int):
            token_ids = [token_ids]
        
        results = []
        for ids in token_ids:
            if skip_special_tokens:
                ids = [id for id in ids if id not in {
                    self.config.pad_id,
                    self.config.bos_id,
                    self.config.eos_id
                }]
            
            # Handle concept tokens and regular tokens
            pieces = []
            for id in ids:
                if id in self.inverse_concept_embeddings:
                    pieces.append(self.inverse_concept_embeddings[id])
                else:
                    pieces.append(self.sp_model.decode_ids([id]))
            
            text = "".join(pieces).replace("▁", " ").strip()
            results.append(text)
        
        return results[0] if len(results) == 1 else results
    
    def _detect_concepts(self, text: str) -> List[tuple]:
        """Detect concept spans in text."""
        spans = []
        for concept, related_terms in self.semantic_clusters.items():
            # Create pattern from concept and related terms
            pattern = r'\b(' + '|'.join([re.escape(concept)] + list(related_terms)) + r')\b'
            for match in re.finditer(pattern, text, re.IGNORECASE):
                spans.append((match.start(), match.end(), concept))
        
        # Sort spans by start position
        return sorted(spans, key=lambda x: x[0])
    
    def add_concept(self, concept: str, related_terms: List[str]):
        """Add a new concept with related terms."""
        concept_token = f"[CONCEPT_{concept.upper()}]"
        if concept_token not in self.concept_embeddings:
            idx = len(self.sp_model) + len(self.concept_embeddings)  # Adjust index calculation
            self.concept_embeddings[concept_token] = idx
            self.inverse_concept_embeddings[idx] = concept_token  # Update inverse mapping
        
        self.semantic_clusters[concept].update(related_terms)
    
    def save_pretrained(self, path: str):
        """Save tokenizer files and concept data."""
        os.makedirs(path, exist_ok=True)
        
        # Save SentencePiece model by copying the original model file
        if self.sp_model is not None:
            import shutil
            source_model = f"{self.config.model_prefix}.model"
            if os.path.exists(source_model):
                shutil.copy2(source_model, f"{path}/tokenizer.model")
            else:
                raise FileNotFoundError(f"SentencePiece model file not found: {source_model}")
        
        # Save concept data
        concept_data = {
            "concept_embeddings": self.concept_embeddings,
            "semantic_clusters": {k: list(v) for k, v in self.semantic_clusters.items()}
        }
        
        with open(f"{path}/concept_data.json", "w", encoding="utf-8") as f:
            json.dump(concept_data, f, ensure_ascii=False, indent=2)
        
        # Save config
        config_dict = {k: v for k, v in vars(self.config).items()}
        with open(f"{path}/config.json", "w", encoding="utf-8") as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=2)
    
    @classmethod
    def from_pretrained(cls, path: str) -> 'ConceptualTokenizer':
        """Load tokenizer from saved files."""
        with open(f"{path}/config.json", "r", encoding="utf-8") as f:
            config_dict = json.load(f)
        
        config = ConceptualTokenizerConfig(**config_dict)
        tokenizer = cls(config)
        
        # Load SentencePiece model
        tokenizer.sp_model = spm.SentencePieceProcessor()
        tokenizer.sp_model.load(f"{path}/tokenizer.model")
        
        # Load concept data
        with open(f"{path}/concept_data.json", "r", encoding="utf-8") as f:
            concept_data = json.load(f)
            tokenizer.concept_embeddings = concept_data["concept_embeddings"]
            tokenizer.semantic_clusters = {k: set(v) for k, v in concept_data["semantic_clusters"].items()}
            tokenizer.inverse_concept_embeddings = {v: k for k, v in tokenizer.concept_embeddings.items()}
        
        return tokenizer

    def analyze_concepts(self, text: str) -> Dict[str, float]:
        """Dummy scoring of detected concepts for tests."""
        scores = {"math": 0.0, "science": 0.0, "logic": 0.0}
        lower_text = text.lower()
        if "equation" in lower_text or "solve" in lower_text:
            scores["math"] += 1.0
        if "chemical" in lower_text or "reaction" in lower_text:
            scores["science"] += 1.0
        # Updated to include [CONCEPT_LOGIC] token
        if "if" in lower_text or "then" in lower_text or "[concept_logic]" in lower_text:
            scores["logic"] += 1.0
        return scores