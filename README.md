so basically i made this at work because i didn't really want to change all the files to the invoice number by hand because that sucks and ai is literally taking over jobs so like why cant i make this and make it free so people dont need to rely on shady web apps or paid saas



make sure you read the requirements cus ts is not gonna work at all without those libraries



make sure you input the correct tesseract and poppler directories or else the script will not work

same applies for the i/o directories



so what you wanna do first is scan a document and then the script reads it and uses ocr to find the invoice number, it should be 6 numbers but if its not ill see if i can add better support



after you scan the document you will be able to see the original file in invoice-input and the new file with the invoice number as the name in input-output

idk what else to add here



run runinvoicerenamergui.bat instead of runinvoicerenamer.bat, it opens a small window where you can:



see/change the watch (input) and output folders with a Browse button, no more editing config.json by hand for that

click Start Watching / Stop instead of leaving a terminal open

see the same status messages that used to print to the terminal, now in a log box

the old terminal version (runinvoicerenamer.bat) still works exactly the same if you prefer it



update: it used to just delete the original scan from invoice-input once it copied the renamed one to invoice-output, now it asks you first

after each file gets renamed it'll pop up asking "delete the original scan?" - say yes and it deletes it from invoice-input like before, say no and it just stays there so you still have the raw scan around

this happens in both the gui and the terminal version (terminal just asks with a y/n prompt instead of a popup)



oh yeah i made this entire thing on notepad

