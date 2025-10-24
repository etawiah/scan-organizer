#!/usr/bin/env python3
"""
Automated Scan Organizer with OCR and AI Labeling
Monitors a folder for scanned documents, performs OCR, and organizes them intelligently.
"""

# ============================================================================
# WINDOWS CONFIGURATION (Uncomment and set paths if needed)
# ============================================================================
# If Tesseract or Poppler are not in your PATH, uncomment and set these:

# import pytesseract
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# import os
# os.environ['PATH'] += r';C:\poppler-25.07.0\Library\bin'

# ============================================================================

import os
import sys
import time
import json
from datetime import datetime
from pathlib import Path
import shutil
import re

try:
    import pytesseract
    from PIL import Image
    from pdf2image import convert_from_path
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError as e:
    print(f"Missing required package: {e}")
    print("\nPlease install required packages:")
    print("pip install pytesseract pillow pdf2image watchdog --break-system-packages")
    print("sudo apt-get install tesseract-ocr poppler-utils")
    sys.exit(1)


class ScanProcessor:
    """Handles OCR and AI-powered file organization"""
    
    SUPPORTED_IMAGE_FORMATS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.gif'}
    SUPPORTED_PDF_FORMAT = '.pdf'
    
    CATEGORIES = [
        "receipts",
        "invoices", 
        "bills",
        "letters",
        "documents",
        "mail",
        "pictures",
        "other"
    ]
    
    def __init__(self, scan_folder):
        self.scan_folder = Path(scan_folder)
        if not self.scan_folder.exists():
            raise ValueError(f"Scan folder does not exist: {scan_folder}")
    
    def extract_text_from_image(self, image_path):
        """Extract text from an image using Tesseract OCR"""
        try:
            image = Image.open(image_path)
            text = pytesseract.image_to_string(image)
            return text.strip()
        except Exception as e:
            print(f"Error extracting text from image {image_path}: {e}")
            return ""
    
    def extract_text_from_pdf(self, pdf_path):
        """Extract text from a PDF using OCR"""
        try:
            # Convert PDF to images
            images = convert_from_path(pdf_path, dpi=300, first_page=1, last_page=3)
            
            # Extract text from first few pages
            text_parts = []
            for i, image in enumerate(images[:3]):  # Limit to first 3 pages
                text = pytesseract.image_to_string(image)
                text_parts.append(text)
            
            return "\n\n".join(text_parts).strip()
        except Exception as e:
            print(f"Error extracting text from PDF {pdf_path}: {e}")
            return ""
    
    def get_ai_classification(self, text_content, original_filename):
        """Use Claude API to classify and generate filename"""
        try:
            import urllib.request
            
            # Truncate text if too long
            max_chars = 3000
            if len(text_content) > max_chars:
                text_content = text_content[:max_chars] + "..."
            
            prompt = f"""Analyze this scanned document and provide classification and naming information.

Original filename: {original_filename}

Document text:
{text_content}

Based on the content, provide a JSON response with:
1. "category" - one of: receipts, invoices, bills, letters, documents, mail, pictures, other
2. "suggested_name" - a clear, descriptive filename (no extension, use underscores not spaces, keep it concise 2-4 words)
3. "confidence" - your confidence level (high/medium/low)

Examples of good names:
- walmart_grocery_receipt
- electric_bill_december
- job_offer_letter
- tax_document_w2
- insurance_claim_form

Respond ONLY with valid JSON in this exact format:
{{"category": "receipts", "suggested_name": "target_receipt", "confidence": "high"}}

DO NOT include any text outside the JSON. DO NOT use markdown code blocks."""

            request_body = {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [
                    {"role": "user", "content": prompt}
                ]
            }
            
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(request_body).encode('utf-8'),
                headers={
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                response_text = result['content'][0]['text'].strip()
                
                # Clean up response - remove markdown code blocks if present
                response_text = re.sub(r'```json\s*', '', response_text)
                response_text = re.sub(r'```\s*', '', response_text)
                response_text = response_text.strip()
                
                classification = json.loads(response_text)
                return classification
                
        except Exception as e:
            print(f"AI classification unavailable (using fallback): {e}")
            # Use simple keyword-based fallback classification
            return self.fallback_classification(text_content, original_filename)
    
    def fallback_classification(self, text_content, original_filename):
        """Fallback classification using keyword matching when AI is unavailable"""
        text_lower = (text_content + " " + original_filename).lower()
        
        # Simple keyword matching
        if any(word in text_lower for word in ['receipt', 'paid', 'purchase', 'transaction', 'sale']):
            return {"category": "receipts", "suggested_name": "receipt", "confidence": "low"}
        elif any(word in text_lower for word in ['invoice', 'inv', 'bill to', 'amount due']):
            return {"category": "invoices", "suggested_name": "invoice", "confidence": "low"}
        elif any(word in text_lower for word in ['bill', 'statement', 'account', 'balance due']):
            return {"category": "bills", "suggested_name": "bill", "confidence": "low"}
        elif any(word in text_lower for word in ['letter', 'dear', 'sincerely', 'regards']):
            return {"category": "letters", "suggested_name": "letter", "confidence": "low"}
        elif any(word in text_lower for word in ['mail', 'postage', 'usps', 'fedex', 'ups']):
            return {"category": "mail", "suggested_name": "mail", "confidence": "low"}
        elif any(word in text_lower for word in ['.jpg', '.jpeg', '.png', 'photo', 'image', 'picture']):
            return {"category": "pictures", "suggested_name": "picture", "confidence": "low"}
        else:
            return {"category": "documents", "suggested_name": "document", "confidence": "low"}
    
    def sanitize_filename(self, filename):
        """Clean up filename to be filesystem-safe"""
        # Remove or replace invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Replace spaces with underscores
        filename = filename.replace(' ', '_')
        # Remove multiple underscores
        filename = re.sub(r'_+', '_', filename)
        # Remove leading/trailing underscores
        filename = filename.strip('_')
        # Limit length
        if len(filename) > 100:
            filename = filename[:100]
        return filename.lower()
    
    def process_file(self, file_path):
        """Process a single scanned file"""
        file_path = Path(file_path)
        
        # Skip if file is in a subdirectory (already processed)
        if file_path.parent != self.scan_folder:
            return
        
        print(f"\n{'='*60}")
        print(f"Processing: {file_path.name}")
        print(f"{'='*60}")
        
        # Get file extension
        extension = file_path.suffix.lower()
        
        # Check if supported format
        if extension not in self.SUPPORTED_IMAGE_FORMATS and extension != self.SUPPORTED_PDF_FORMAT:
            print(f"Skipping unsupported format: {extension}")
            return
        
        # Get file creation date
        creation_time = os.path.getctime(file_path)
        creation_date = datetime.fromtimestamp(creation_time)
        date_suffix = creation_date.strftime("%m%d%Y")
        
        # Extract text based on file type
        print("Extracting text via OCR...")
        if extension == self.SUPPORTED_PDF_FORMAT:
            text = self.extract_text_from_pdf(file_path)
        else:
            text = self.extract_text_from_image(file_path)
        
        if not text:
            print("Warning: No text extracted from document")
            text = f"File: {file_path.name}"
        
        print(f"Extracted {len(text)} characters of text")
        
        # Get AI classification
        print("Classifying with AI...")
        classification = self.get_ai_classification(text, file_path.name)
        
        category = classification.get('category', 'other')
        suggested_name = classification.get('suggested_name', 'document')
        confidence = classification.get('confidence', 'low')
        
        # Sanitize the suggested name
        suggested_name = self.sanitize_filename(suggested_name)
        
        # Create new filename
        new_filename = f"{suggested_name}_{date_suffix}{extension}"
        
        # Create category folder if it doesn't exist
        category_folder = self.scan_folder / category
        category_folder.mkdir(exist_ok=True)
        
        # Destination path
        dest_path = category_folder / new_filename
        
        # Handle filename conflicts
        counter = 1
        while dest_path.exists():
            new_filename = f"{suggested_name}_{date_suffix}_{counter}{extension}"
            dest_path = category_folder / new_filename
            counter += 1
        
        # Move file
        try:
            shutil.move(str(file_path), str(dest_path))
            print(f"\n✓ SUCCESS")
            print(f"  Category: {category} (confidence: {confidence})")
            print(f"  New name: {new_filename}")
            print(f"  Location: {category}/")
        except Exception as e:
            print(f"\n✗ FAILED to move file: {e}")
    
    def process_existing_files(self):
        """Process all existing files in the scan folder"""
        print(f"\nScanning folder: {self.scan_folder}")
        print(f"Looking for files to process...\n")
        
        files_to_process = []
        for file_path in self.scan_folder.iterdir():
            if file_path.is_file():
                extension = file_path.suffix.lower()
                if extension in self.SUPPORTED_IMAGE_FORMATS or extension == self.SUPPORTED_PDF_FORMAT:
                    files_to_process.append(file_path)
        
        if not files_to_process:
            print("No files found to process.")
            return
        
        print(f"Found {len(files_to_process)} file(s) to process\n")
        
        for file_path in files_to_process:
            self.process_file(file_path)
            time.sleep(1)  # Small delay between files


class ScanFolderHandler(FileSystemEventHandler):
    """Watches for new files in the scan folder"""
    
    def __init__(self, processor):
        self.processor = processor
        self.processing_files = set()
    
    def on_created(self, event):
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        
        # Skip if already processing
        if file_path in self.processing_files:
            return
        
        # Skip temporary files
        if file_path.name.startswith('.') or file_path.name.startswith('~'):
            return
        
        # Wait a moment for file to finish writing
        time.sleep(2)
        
        # Check if file still exists and is readable
        if not file_path.exists():
            return
        
        try:
            self.processing_files.add(file_path)
            self.processor.process_file(file_path)
        finally:
            self.processing_files.discard(file_path)


def main():
    """Main entry point"""
    print("\n" + "="*60)
    print("AUTOMATED SCAN ORGANIZER")
    print("="*60)
    
    # Get scan folder from command line or use default
    if len(sys.argv) > 1:
        scan_folder = sys.argv[1]
    else:
        scan_folder = input("\nEnter the path to your scan folder: ").strip()
    
    if not scan_folder:
        print("Error: No scan folder provided")
        sys.exit(1)
    
    # Initialize processor
    try:
        processor = ScanProcessor(scan_folder)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    
    # Ask user what they want to do
    print("\nOptions:")
    print("1. Process existing files only")
    print("2. Watch folder for new files (continuous)")
    print("3. Both - process existing files then watch")
    
    choice = input("\nChoose option (1/2/3): ").strip()
    
    if choice in ['1', '3']:
        processor.process_existing_files()
    
    if choice in ['2', '3']:
        print("\n" + "="*60)
        print("STARTING FOLDER MONITOR")
        print("="*60)
        print(f"\nWatching: {scan_folder}")
        print("Press Ctrl+C to stop\n")
        
        event_handler = ScanFolderHandler(processor)
        observer = Observer()
        observer.schedule(event_handler, scan_folder, recursive=False)
        observer.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nStopping folder monitor...")
            observer.stop()
        
        observer.join()
        print("Monitor stopped.")


if __name__ == "__main__":
    main()
