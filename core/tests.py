from decimal import Decimal as D
from datetime import datetime, timedelta

from django.test import TestCase
from django.test.client import Client
from django.utils import unittest
from django.db import IntegrityError
from django.contrib.auth.models import User
from django.conf import settings
from django.contrib.comments.models import Comment
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site

from regluit.payment.models import Transaction
from regluit.core.models import Campaign, Work, UnglueitError
from regluit.core import bookloader, models, search, goodreads, librarything
from regluit.core import isbn
from regluit.payment.parameters import PAYMENT_TYPE_AUTHORIZATION

from regluit.core import tasks
from celery.task.sets import TaskSet
from celery.task import chord

from time import sleep
from math import factorial


class BookLoaderTests(TestCase):

    def test_add_by_isbn(self):
        # edition
        edition = bookloader.add_by_isbn('0441012035')
        self.assertEqual(edition.title, 'Neuromancer')
        self.assertEqual(edition.publication_date, '2004')
        self.assertEqual(edition.publisher, u'Ace Hardcover')
        self.assertEqual(edition.isbn_10, '0441012035')
        self.assertEqual(edition.isbn_13, '9780441012039')
        self.assertEqual(edition.googlebooks_id, "2NyiPwAACAAJ")

        # authors
        self.assertEqual(edition.authors.all().count(), 1)
        self.assertEqual(edition.authors.all()[0].name, 'William Gibson')

        # work
        self.assertTrue(edition.work)
        
        # locale in language
        edition = bookloader.add_by_isbn('9787500676911')
        self.assertEqual(edition.work.language, 'zh')

    def test_update_edition(self):  
        w = models.Work(title='silly title', language='xx')
        w.save()
        e = models.Edition(title=w.title,work=w)
        e.save()
        models.Identifier(type='isbn', value='9780226032030', work=w, edition=e).save()
        bookloader.update_edition(e)
        self.assertEqual(e.work.language, 'en')
        self.assertEqual(e.title, 'Forbidden journeys')

    def test_double_add(self):
        bookloader.add_by_isbn('0441012035')
        bookloader.add_by_isbn('0441012035')
        self.assertEqual(models.Edition.objects.all().count(), 1)
        self.assertEqual(models.Author.objects.all().count(), 1)
        self.assertEqual(models.Work.objects.all().count(), 1)
       
    def test_missing_isbn(self):
        e = bookloader.add_by_isbn_from_google('0139391401')
        self.assertEqual(e, None)

    def test_thingisbn(self):
        isbns = bookloader.thingisbn('0441012035')
        self.assertTrue(len(isbns) > 20)
        self.assertTrue('0441012035' in isbns)
        self.assertTrue('3453313895' in isbns)

    def test_add_related(self):
        # add one edition
        edition = bookloader.add_by_isbn('0441012035')
        self.assertEqual(models.Edition.objects.count(), 1)
        self.assertEqual(models.Work.objects.count(), 1)
        lang=edition.work.language
        # ask for related editions to be added using the work we just created
        bookloader.add_related('0441012035')
        self.assertTrue(models.Edition.objects.count() > 15)
        self.assertEqual(models.Work.objects.filter(language=lang).count(), 1)
        self.assertTrue(edition.work.editions.count() > 9)


    def test_populate_edition(self):
        edition = bookloader.add_by_googlebooks_id('c_dBPgAACAAJ')
        edition = tasks.populate_edition.run(edition)
        self.assertTrue(edition.work.editions.all().count() > 20)
        self.assertTrue(edition.work.subjects.all().count() > 10)
        self.assertTrue(edition.work.publication_date)
        edition.publication_date = None
        self.assertTrue(edition.work.publication_date)

    def test_merge_works(self):
        # add two editions and see that there are two stub works
        e1 = bookloader.add_by_isbn('0465019358')
        e2 = bookloader.add_by_isbn('1458776204')
        self.assertTrue(e1)
        self.assertTrue(e2)
        self.assertTrue(e1.work)
        self.assertTrue(e2.work)
        self.assertEqual(models.Work.objects.count(), 2)

        # add the stub works to a wishlist
        user = User.objects.create_user('test', 'test@example.com', 'testpass')
        user.wishlist.add_work(e1.work, 'test')
        user.wishlist.add_work(e2.work, 'test')

        # create campaigns for the stub works 
        c1 = models.Campaign.objects.create(
            name=e1.work.title,
            work=e2.work, 
            description='Test Campaign 1',
            deadline=datetime.now(),
            target=D('1000.00'),
        )
        c2 = models.Campaign.objects.create(
            name=e2.work.title,
            work=e2.work, 
            description='Test Campaign 2',
            deadline=datetime.now(),
            target=D('1000.00'),
        )
        
        # comment on the works
        site = Site.objects.all()[0]
        wct = ContentType.objects.get_for_model(models.Work)
        comment1 = Comment(
            content_type=wct,
            object_pk=e1.work.pk,
            comment="test comment1",
            user=user, 
            site=site
        )
        comment1.save()
        comment2 = Comment(
            content_type=wct,
            object_pk=e2.work.pk,
            comment="test comment2",
            user=user, 
            site=site
        )
        comment2.save()
        
        # now add related edition to make sure Works get merged
        bookloader.add_related('1458776204')
        self.assertEqual(models.Work.objects.count(), 1)
        w3 = models.Edition.get_by_isbn('1458776204').work
        
        # and that relevant Campaigns and Wishlists are updated
        
        self.assertEqual(c1.work, c2.work)
        self.assertEqual(user.wishlist.works.all().count(), 1)
        self.assertEqual(Comment.objects.for_model(w3).count(), 2)
        
        anon_client = Client()
        r = anon_client.get("/work/%s/" % w3.pk)
        self.assertEqual(r.status_code, 200)
        r = anon_client.get("/work/%s/" % e2.work.pk)
        self.assertEqual(r.status_code, 200)

    
    def test_ebook(self):
        edition = bookloader.add_by_oclc('1246014')
        self.assertEqual(edition.ebooks.count(), 2)
        #ebook_epub = edition.ebooks.all()[0]
        ebook_epub = edition.ebooks.filter(format='epub')[0]
        self.assertEqual(ebook_epub.format, 'epub')
        self.assertEqual(ebook_epub.url, 'http://books.google.com/books/download/The_Latin_language.epub?id=U3FXAAAAYAAJ&ie=ISO-8859-1&output=epub&source=gbs_api')
        self.assertEqual(ebook_epub.provider, 'google')
        ebook_pdf = edition.ebooks.filter(format='pdf')[0]
        self.assertEqual(ebook_pdf.format, 'pdf')
        self.assertEqual(ebook_pdf.url, 'http://books.google.com/books/download/The_Latin_language.pdf?id=U3FXAAAAYAAJ&ie=ISO-8859-1&output=pdf&sig=ACfU3U2yLt3nmTncB8ozxOWUc4iHKUznCA&source=gbs_api')
        self.assertEqual(ebook_pdf.provider, 'google')        

        w = edition.work
        self.assertEqual(w.first_epub().url, "http://books.google.com/books/download/The_Latin_language.epub?id=U3FXAAAAYAAJ&ie=ISO-8859-1&output=epub&source=gbs_api")
        self.assertEqual(w.first_pdf().url, "http://books.google.com/books/download/The_Latin_language.pdf?id=U3FXAAAAYAAJ&ie=ISO-8859-1&output=pdf&sig=ACfU3U2yLt3nmTncB8ozxOWUc4iHKUznCA&source=gbs_api")
        self.assertEqual(w.first_epub_url(), "http://books.google.com/books/download/The_Latin_language.epub?id=U3FXAAAAYAAJ&ie=ISO-8859-1&output=epub&source=gbs_api")
        self.assertEqual(w.first_pdf_url(), "http://books.google.com/books/download/The_Latin_language.pdf?id=U3FXAAAAYAAJ&ie=ISO-8859-1&output=pdf&sig=ACfU3U2yLt3nmTncB8ozxOWUc4iHKUznCA&source=gbs_api")

    def test_add_no_ebook(self):
        # this edition lacks an ebook, but we should still be able to load it
        e = bookloader.add_by_isbn('0465019358')
        self.assertTrue(e)

    def test_one_language(self):
        # english edition for cat's cradle should only pull in other 
        # english editions
        work = bookloader.add_by_isbn('079530272X').work
        self.assertEqual(work.language, 'en')
        bookloader.add_related('079530272X')
        for edition in work.editions.all():
            self.assertEqual(edition.title.lower(), "cat's cradle")

    def test_add_openlibrary(self):
        work = bookloader.add_by_isbn('0441012035').work
        bookloader.add_related('0441012035')
        bookloader.add_openlibrary(work)
        subjects = [s.name for s in work.subjects.all()]
        self.assertTrue(len(subjects) > 10)
        self.assertTrue('Science fiction' in subjects)
        self.assertEqual(work.openlibrary_id, '/works/OL27258W')
        self.assertEqual(work.goodreads_id, '14770')
        self.assertEqual(work.librarything_id, '609')
    def test_load_gutenberg_edition(self):
        """Let's try this out for Moby Dick"""
        
        title = "Moby Dick"
        ol_work_id = "/works/OL102749W"
        gutenberg_etext_id = 2701
        epub_url = "http://www.gutenberg.org/cache/epub/2701/pg2701.epub"
        license = 'http://www.gutenberg.org/license'
        lang = 'en'
        format = 'epub'
        publication_date = datetime(2001,7,1)
        seed_isbn = '9780142000083' # http://www.amazon.com/Moby-Dick-Whale-Penguin-Classics-Deluxe/dp/0142000086
        
        ebook = bookloader.load_gutenberg_edition(title, gutenberg_etext_id, ol_work_id, seed_isbn, epub_url, format, license, lang, publication_date)
        self.assertEqual(ebook.url, epub_url)
        
        
        


class SearchTests(TestCase):

    def test_basic_search(self):
        results = search.gluejar_search('melville')
        self.assertEqual(len(results), 10)

        r = results[0]
        self.assertTrue(r.has_key('title'))
        self.assertTrue(r.has_key('author'))
        self.assertTrue(r.has_key('description'))
        self.assertTrue(r.has_key('cover_image_thumbnail'))
        self.assertTrue(r.has_key('publisher'))
        self.assertTrue(r.has_key('isbn_13'))
        self.assertTrue(r.has_key('googlebooks_id'))

    def test_pagination(self):
        r1 = search.gluejar_search('melville', page=1)
        r2 = search.gluejar_search('melville', page=2)
        isbns1 = set([r['isbn_13'] for r in r1])
        isbns2 = set([r['isbn_13'] for r in r2])
        self.assertTrue(isbns1 != isbns2)

    def test_googlebooks_search(self):
        response = search.googlebooks_search('melville', '69.243.24.29', 1)
        self.assertEqual(len(response['items']), 10)


class CampaignTests(TestCase):

    def test_required_fields(self):
        # a campaign must have a target, deadline and a work

        c = Campaign()
        self.assertRaises(IntegrityError, c.save)

        c = Campaign(target=D('1000.00'))
        self.assertRaises(IntegrityError, c.save)

        c = Campaign(target=D('1000.00'), deadline=datetime(2012, 1, 1))
        self.assertRaises(IntegrityError, c.save)

        w = Work()
        w.save()
        c = Campaign(target=D('1000.00'), deadline=datetime(2012, 1, 1), work=w)
        c.save()

    def test_campaign_status(self):
        w = Work()
        w.save()
        # INITIALIZED
        c1 = Campaign(target=D('1000.00'),deadline=datetime(2012,1,1),work=w)
        c1.save()
        self.assertEqual(c1.status, 'INITIALIZED')
        # ACTIVATED
        c2 = Campaign(target=D('1000.00'),deadline=datetime(2012,1,1),work=w)
        c2.save()
        self.assertEqual(c2.status, 'INITIALIZED')
        c2.activate()
        self.assertEqual(c2.status, 'ACTIVE')
        # SUSPENDED
        c2.suspend(reason="for testing")
        self.assertEqual(c2.status, 'SUSPENDED')
        # RESUMING
        c2.resume(reason="for testing")
        #self.assertEqual(c2.suspended, None)
        self.assertEqual(c2.status,'ACTIVE')
        # should not let me suspend a campaign that hasn't been initialized
        self.assertRaises(UnglueitError, c1.suspend, "for testing")
        # UNSUCCESSFUL
        c3 = Campaign(target=D('1000.00'),deadline=datetime.utcnow() - timedelta(days=1),work=w)
        c3.save()
        c3.activate()
        self.assertTrue(c3.update_success())
        self.assertEqual(c3.status, 'UNSUCCESSFUL')
            
        # SUCCESSFUL
        c4 = Campaign(target=D('1000.00'),deadline=datetime.utcnow() - timedelta(days=1),work=w)
        c4.save()
        c4.activate()
        t = Transaction()
        t.amount = D('1234.00')
        t.type = PAYMENT_TYPE_AUTHORIZATION
        t.status = 'ACTIVE'
        t.approved = True
        t.campaign = c4
        t.save()        
        self.assertTrue(c4.update_success())        
        self.assertEqual(c4.status, 'SUCCESSFUL')
        
        # WITHDRAWN
        c5 = Campaign(target=D('1000.00'),deadline=datetime(2012,1,1),work=w)
        c5.save()
        c5.activate().withdraw('testing')
        self.assertEqual(c5.status, 'WITHDRAWN')        


class WishlistTest(TestCase):

    def test_add_remove(self):
        # add a work to a user's wishlist
        user = User.objects.create_user('test', 'test@example.com', 'testpass')
        edition = bookloader.add_by_isbn('0441012035')
        work = edition.work
        num_wishes=work.num_wishes
        user.wishlist.add_work(work, 'test')
        self.assertEqual(user.wishlist.works.count(), 1)
        self.assertEqual(work.num_wishes, num_wishes+1)
        user.wishlist.remove_work(work)
        self.assertEqual(user.wishlist.works.count(), 0)
        self.assertEqual(work.num_wishes, num_wishes)
        
class CeleryTaskTest(TestCase):

    def test_single_fac(self):
        n = 10
        task = tasks.fac.delay(n)
        result = task.get(timeout=10)
        self.assertEqual(result,factorial(n))

    def test_subtask(self):
        n = 30
        subtasks = [tasks.fac.subtask(args=(x,)) for x in range(n)]
        job = TaskSet(tasks=subtasks)
        result = job.apply_async()
        while not result.ready():
            sleep(0.2)
        self.assertEqual(result.join(),[factorial(x) for x in range(n)])
    
class GoodreadsTest(TestCase):

    def test_goodreads_shelves(self):
        # test to see whether the core undeletable shelves are on the list
        gr_uid = "767708"  # for Raymond Yee
        gc = goodreads.GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        shelves = gc.shelves_list(gr_uid)
        shelf_names = [s['name'] for s in shelves['user_shelves']]
        self.assertTrue('currently-reading' in shelf_names)
        self.assertTrue('read' in shelf_names)
        self.assertTrue('to-read' in shelf_names)

    def test_review_list_unauth(self):
        gr_uid = "767708"  # for Raymond Yee
        gc = goodreads.GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        reviews = gc.review_list_unauth(user_id=gr_uid, shelf='read')
        # test to see whether there is a book field in each of the review
        self.assertTrue(all([r.has_key("book") for r in reviews]))

class LibraryThingTest(TestCase):

    def test_scrape_test_lib(self):
        # account yujx : has one book: 0471925675
        lt_username = 'yujx'
        lt = librarything.LibraryThing(username=lt_username)
        books = list(lt.parse_user_catalog(view_style=5))
        self.assertEqual(len(books),1)
        self.assertEqual(books[0]['isbn'], '0471925675')
        self.assertEqual(books[0]['work_id'], '80826')
        self.assertEqual(books[0]['book_id'], '79883733')

class ISBNTest(TestCase):

    def test_ISBN(self):
        milosz_10 = '006019667X'
        milosz_13 = '9780060196677'
        python_10 = '0-672-32978-6'
        python_10_wrong = '0-672-32978-7'
        python_13 = '978-0-672-32978-4'
        
        isbn_python_10 = isbn.ISBN(python_10)
        isbn_python_13 = isbn.ISBN(python_13)
        # return None for invalid characters
        self.assertEqual(None, isbn.ISBN("978-0-M72-32978-X").to_string('13'))
        self.assertEqual(isbn.ISBN("978-0-M72-32978-X").valid, False)
        # check that only ISBN 13 starting with 978 or 979 are accepted
        self.assertEqual(None, isbn.ISBN("111-0-M72-32978-X").to_string())
        
        # right type?
        self.assertEqual(isbn_python_10.type, '10')
        self.assertEqual(isbn_python_13.type, '13')
        # valid?
        self.assertEqual(isbn_python_10.valid, True)
        self.assertEqual(isbn.ISBN(python_10_wrong).valid, False)
        
        # do conversion -- first the outside methods
        self.assertEqual(isbn.convert_10_to_13(isbn.strip(python_10)),isbn.strip(python_13))
        self.assertEqual(isbn.convert_13_to_10(isbn.strip(python_13)),isbn.strip(python_10))
        
        # check formatting
        self.assertEqual(isbn.ISBN(python_13).to_string(type='13'), '9780672329784')
        self.assertEqual(isbn.ISBN(python_13).to_string('13',True), '978-0-672-32978-4')
        self.assertEqual(isbn.ISBN(python_13).to_string(type='10'), '0672329786')
        self.assertEqual(isbn.ISBN(python_10).to_string(type='13'), '9780672329784')
        self.assertEqual(isbn.ISBN(python_10).to_string(10,True), '0-672-32978-6')
        
        # complain if one tries to get ISBN-10 for a 979 ISBN 13
        # making up a 979 ISBN
        isbn_979 = isbn.ISBN("979-1-234-56789-0").validate()
        self.assertEqual(isbn_979.to_string('10'), None)
        
        # check casting to string -- ISBN 13
        self.assertEqual(str(isbn.ISBN(python_10)), '0672329786')
        
        # test __eq__ and __ne__ and validate
        self.assertTrue(isbn.ISBN(milosz_10) == isbn.ISBN(milosz_13))
        self.assertTrue(isbn.ISBN(milosz_10) == milosz_13)
        self.assertFalse(isbn.ISBN(milosz_10) == 'ddds')
        self.assertFalse(isbn.ISBN(milosz_10) != milosz_10)
        self.assertTrue(isbn.ISBN(python_10) != python_10_wrong)
        self.assertEqual(isbn.ISBN(python_10_wrong).validate(), python_10)
        self.assertEqual(isbn.ISBN(python_13).validate(), python_10)
        
        # curious about set membership
        self.assertEqual(len(set([isbn.ISBN(milosz_10), isbn.ISBN(milosz_13)])),2)
        self.assertEqual(len(set([str(isbn.ISBN(milosz_10)), str(isbn.ISBN(milosz_13))])),2)
        self.assertEqual(len(set([isbn.ISBN(milosz_10).to_string(), isbn.ISBN(milosz_13).to_string()])),1)

