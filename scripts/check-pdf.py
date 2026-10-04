"""Independent PDF text, page bounds, and ZIP validation after manual.test.mjs."""
import json,pathlib,zipfile,sys
import pdfplumber
from pypdf import PdfReader
root=pathlib.Path('tmp/pdfs')
for name in sys.argv[1:] or ['default','custom','secondary','mixed']:
    meta=json.loads((root/f'{name}.json').read_text());reader=PdfReader(root/f'{name}.pdf');text='\n'.join(p.extract_text() for p in reader.pages)
    for phrase in [f"{meta['diameter']} mm",meta['build'],'exploded view','Printed parts','Hardware / quantities','Build record']:
        assert phrase in text,(name,phrase)
    for p in meta['parts']:
        if p.get('printIncluded') is False:assert 'Reuse compatible existing parts' in text;continue
        assert f"{p['id']}_qty-{p['qty']}.stl" in text,(name,p)
    if meta['feed']:assert meta['feed']['cut'] in text and 'Rod cuts / feed placement' in text
    with pdfplumber.open(root/f'{name}.pdf') as pdf:
        for i,page in enumerate(pdf.pages):
            for ch in page.chars:
                assert ch['x0']>=30 and ch['x1']<=page.width-30,(name,i,'horizontal overflow',ch)
                color=ch.get('non_stroking_color')
                assert not (isinstance(color,(tuple,list)) and len(color)==3 and min(color)>.98 and ch['top']>66 and ch['text'].isalpha()),(name,i,'white body text',ch)
                assert ch['top']>=8 and ch['bottom']<=page.height-15,(name,i,'vertical overflow',ch)
    print('PASS PDF content and page bounds:',name,len(reader.pages),'pages')
with zipfile.ZipFile(root/'kit.zip') as z:
    assert z.testzip() is None
    assert z.read('ASSEMBLY.pdf')[:5]==b'%PDF-'
    assert 'ASSEMBLY.md' in z.namelist()
    PdfReader(__import__('io').BytesIO(z.read('ASSEMBLY.pdf')))
print('PASS ZIP contains a readable illustrated ASSEMBLY.pdf and original instructions')
