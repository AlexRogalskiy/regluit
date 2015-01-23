"""
Utility for calling mobigen

"""

from itertools import islice
from StringIO import StringIO
import requests
import uuid

from django.core.files.storage import default_storage
from django.core.files.base import ContentFile, File

from regluit.core.models import Campaign


def convert_to_mobi(input_url, input_format="application/epub+zip"):
    
    """
    return a string with the output of mobigen computation
    
    """


    # substitute file_path with a local epub or html file
    #file_path = "/Users/raymondyee/D/Document/Gluejar/Gluejar.github/regluit/test-data/pg2701.epub"
    #file_type = "application/epub+zip"  

    # where to write the output 
    #output_path = "/Users/raymondyee/Downloads/pg2701.mobi"

    # url of the mobigen service
    mobigen_url = "https://docker.gluejar.com:5001/mobigen"
    mobigen_user_id = "admin"
    mobigen_password = "CXq5FSEQFgXtP_s"

    # read the file and do a http post
    # equivalent curl
    # curl -k --user "admin:CXq5FSEQFgXtP_s" -X POST -H "Content-Type: application/epub+zip" --data-binary "@/Users/raymondyee/D/Document/Gluejar/Gluejar.github/regluit/test-data/pg2701.epub" https://docker.gluejar.com/mobigen:5001 > pg2701.mobi

    # using verify=False since at the moment, using a self-signed SSL cert.

    payload = requests.get(input_url, verify=False).content 

    headers = {'Content-Type': input_format}
    r = requests.post(mobigen_url, auth=(mobigen_user_id, mobigen_password),
                      data=payload, verify=False, headers=headers)

    # if HTTP reponse code is ok, the output is the mobi file; else error message
    if r.status_code == 200:
        return r.content
    else:
        raise Exception("{0}: {1}".format(r.status_code, r.content))


# compute whether we can apply mobigen to a given edition to produce a mobi file
# need to have an ebook in epub or pdf format 
# possible return values:  already has a mobi file / can generate a mobi file / not possible

def edition_mobi_status(edition):
    """
    for a given edition, return:
      * 1 if there is already a mobi ebook
      * 0 if there is none but we have an epub or html to convert from
      * -1 for no epub/html to convert from
    """
    formats = set([ebook.format for ebook in edition.work.ebooks()])
    if 'mobi' in formats:
        return 1
    elif ('epub' in formats) or ('html' in formats):
        return 0
    else:
        return -1
    

def write_file_to_storage(file_object, content_type, path):
    """
    write file_object to the default_storage at given path
    """
    file_s3 = ContentFile(file_object)
    file_s3.content_type = content_type
    
    default_storage.save(path, file_s3)
    return file_s3


# generator for editions to add mobi to
# campaigns that can have mobi files but don't yet.

def editions_to_convert():
    for campaign in Campaign.objects.filter(edition__ebooks__isnull=False).distinct():
        if edition_mobi_status(campaign.edition) == 0: # possible to generate mobi
            yield campaign.edition
            

def generate_mobi_ebook_for_edition(edition):
    
    # pull out the sister edition to convert from
    sister_ebook = edition.ebooks.filter(format__in=['epub', 'html'])[0]
    
    # run the conversion process

    output = convert_to_mobi(sister_ebook.url)
    #output = open("/Users/raymondyee/Downloads/hello.mobi").read()
    
    file_ = write_file_to_storage(output, 
                              "application/x-mobipocket-ebook", 
                              "/ebf/{0}.mobi".format(uuid.uuid4().get_hex()))
    
    # create a path for the ebookfile:   IS THIS NECESSARY?
    # https://github.com/Gluejar/regluit/blob/25dcb06f464dc11b5e589ab6859dfcc487f8f3ef/core/models.py#L1771
    
    #ebfile = EbookFile(edition=edition, file=file_, format='mobi')
    #ebfile.save()

    # maybe need to create an ebook pointing to  ebookFile ?
    # copy metadata from sister ebook
    
    ebfile_url = default_storage.url(file_.name)
    #print (ebfile_url)
    
    ebook = Ebook(url=ebfile_url,
                  format="mobi", 
                  provider="Unglue.it",
                  rights=sister_ebook.rights, 
                  edition=edition)
    ebook.save()
    
    return ebook         
