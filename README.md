# Automated Scan Organizer

An intelligent system that automatically OCRs, categorizes, and organizes your scanned documents using AI.

## Features

- **Automatic OCR**: Extracts text from PDFs and images using Tesseract
- **AI-Powered Classification**: Uses Claude AI to intelligently categorize documents
- **Smart Naming**: Generates meaningful filenames based on content
- **Date Stamping**: Adds scan date (MMDDYYYY) to filenames
- **Auto-Organization**: Creates category folders and moves files automatically
- **Real-time Monitoring**: Can watch folder for new scans continuously

## Supported Categories

- Receipts
- Invoices
- Bills
- Letters
- Documents
- Mail
- Pictures
- Other (catch-all)

## Supported File Formats

- **Images**: JPG, JPEG, PNG, TIFF, TIF, BMP, GIF
- **PDFs**: Multi-page PDF support (OCRs first 3 pages)

## Installation

### Windows

#### 1. Install Tesseract OCR

Download and install Tesseract from: https://github.com/UB-Mannheim/tesseract/wiki

- Download the installer (e.g., `tesseract-ocr-w64-setup-5.3.3.20231005.exe`)
- Run the installer and note the installation path (default: `C:\Program Files\Tesseract-OCR`)
- Add Tesseract to your PATH, or the script will need to know where it is

#### 2. Install Poppler (for PDF support)

Download Poppler for Windows: https://github.com/oschwartz10612/poppler-windows/releases

- Download the latest release (e.g., `Release-24.02.0-0.zip`)
- Extract to a folder like `C:\Program Files\poppler`
- Add the `bin` folder to your PATH: `C:\Program Files\poppler\Library\bin`

#### 3. Install Python Packages

Open Command Prompt or PowerShell:
```cmd
pip install pytesseract pillow pdf2image watchdog
```

#### 4. Configure Tesseract Path (if needed)

If Tesseract isn't in your PATH, add this to the top of `scan_organizer.py` after the imports:
```python
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

#### 5. Run the Script

```cmd
python scan_organizer.py
```

---

### macOS

#### 1. Install Homebrew (if not already installed)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

#### 2. Install Tesseract and Poppler

```bash
brew install tesseract poppler
```

#### 3. Install Python Packages

```bash
pip3 install pytesseract pillow pdf2image watchdog
```

#### 4. Run the Script

```bash
python3 scan_organizer.py
```

---

### Linux (Ubuntu/Debian)

#### 1. Install System Dependencies

```bash
sudo apt-get update
sudo apt-get install tesseract-ocr poppler-utils -y
```

#### 2. Install Python Packages

```bash
pip install pytesseract pillow pdf2image watchdog --break-system-packages
```

#### 3. Run the Script

```bash
python3 scan_organizer.py
```

## Usage

### Interactive Mode

```bash
python3 scan_organizer.py
```

The script will prompt you for:
1. Path to your scan folder
2. What you want to do:
   - Process existing files only
   - Watch for new files (continuous monitoring)
   - Both (process existing + watch)

### Command Line Mode

```bash
# Specify folder directly
python3 scan_organizer.py /path/to/your/scan/folder
```

## How It Works

1. **File Detection**: Finds new scans in your folder
2. **OCR Processing**: Extracts text from the document
3. **AI Analysis**: Claude analyzes the content and determines:
   - What category it belongs to
   - A meaningful filename based on content
   - Confidence level
4. **Organization**: 
   - Creates a subfolder for the category (if needed)
   - Renames file: `meaningful_name_MMDDYYYY.ext`
   - Moves file to appropriate category folder

## Example Output

**Before:**
```
/scans/
  ├── scan_001.pdf
  ├── IMG_2023.jpg
  └── document.pdf
```

**After:**
```
/scans/
  ├── receipts/
  │   └── walmart_grocery_receipt_10232025.pdf
  ├── bills/
  │   └── electric_bill_october_10232025.jpg
  └── documents/
      └── tax_form_w2_10232025.pdf
```

## Tips

- **For best OCR results**: Scan at 300 DPI or higher
- **Network folders**: Make sure you have write permissions
- **Large batches**: The script processes files one at a time with AI analysis (takes 2-5 seconds per file)
- **Continuous monitoring**: Perfect for network scanners that drop files automatically

## Filename Format

Files are renamed using this pattern:
```
{ai_generated_description}_{MMDDYYYY}.{original_extension}
```

Examples:
- `target_grocery_receipt_10232025.pdf`
- `insurance_claim_form_10232025.jpg`
- `bank_statement_september_10232025.pdf`

## Troubleshooting

### "No text extracted from document"
- Check if the scan is clear and readable
- Ensure Tesseract is installed correctly: `tesseract --version`
- Try scanning at higher resolution

### "Error calling AI API"
- The script will fall back to generic naming
- Files will still be moved to "other" category

### Files not being detected
- Make sure files are in the root of the scan folder, not in subdirectories
- Check file permissions
- Verify file format is supported

### Network folder issues
- Ensure the folder is properly mounted
- Check you have read/write permissions
- Test with `touch /path/to/scan/folder/test.txt`

## Advanced: Systemd Service (Linux)

To run continuously as a background service:

1. Create `/etc/systemd/system/scan-organizer.service`:
```ini
[Unit]
Description=Automated Scan Organizer
After=network.target

[Service]
Type=simple
User=your-username
ExecStart=/usr/bin/python3 /path/to/scan_organizer.py /path/to/scan/folder
Restart=always

[Install]
WantedBy=multi-user.target
```

2. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable scan-organizer
sudo systemctl start scan-organizer
```

## Security Note

This script uses the Claude API to analyze document content. The text extracted from your documents is sent to Anthropic's API for classification. Do not use this for highly sensitive documents without reviewing your organization's data policies.

## License

Free to use and modify for personal and commercial use.
