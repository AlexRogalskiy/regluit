import re
import sys
import json
import logging
import urllib

from datetime import timedelta
from regluit.utils.localdatetime import now, date_today

from random import randint
from re import sub
from itertools import islice
from decimal import Decimal as D
from xml.etree import ElementTree as ET
import requests
import oauth2 as oauth
from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.core.exceptions import ObjectDoesNotExist
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.comments import Comment
from django.db.models import Q, Count, Sum
from django.forms import Select
from django.forms.models import modelformset_factory
from django.http import HttpResponseRedirect, Http404
from django.http import HttpResponse, HttpResponseNotFound
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic.edit import FormView
from django.views.generic.list import ListView
from django.views.generic.base import TemplateView
from django.shortcuts import render, render_to_response, get_object_or_404
from django.utils.http import urlencode
from django.utils.translation import ugettext_lazy as _
from regluit.core import tasks
from regluit.core.tasks import send_mail_task
from regluit.core import models, bookloader, librarything
from regluit.core import userlists
from regluit.core.search import gluejar_search
from regluit.core.goodreads import GoodreadsClient
from regluit.frontend.forms import UserData, UserEmail, ProfileForm, CampaignPledgeForm, GoodreadsShelfLoadingForm
from regluit.frontend.forms import  RightsHolderForm, UserClaimForm, LibraryThingForm, OpenCampaignForm
from regluit.frontend.forms import ManageCampaignForm, DonateForm, CampaignAdminForm, EmailShareForm, FeedbackForm
from regluit.frontend.forms import EbookForm, CustomPremiumForm, EditManagersForm
from regluit.payment.manager import PaymentManager
from regluit.payment.models import Transaction
from regluit.payment.parameters import TARGET_TYPE_CAMPAIGN, TARGET_TYPE_DONATION, PAYMENT_TYPE_AUTHORIZATION
from regluit.payment.parameters import TRANSACTION_STATUS_ACTIVE, TRANSACTION_STATUS_COMPLETE_PRIMARY, TRANSACTION_STATUS_CANCELED, TRANSACTION_STATUS_ERROR, TRANSACTION_STATUS_FAILED, TRANSACTION_STATUS_INCOMPLETE
from regluit.payment.paypal import Preapproval
from regluit.core import goodreads
from tastypie.models import ApiKey
from regluit.payment.models import Transaction


logger = logging.getLogger(__name__)

def slideshow(max):
    ending = models.Campaign.objects.filter(status='ACTIVE').order_by('deadline')
    count = ending.count()
    is_preview = settings.IS_PREVIEW
    j = 0
        
    if is_preview:
        # on the preview site there are no active campaigns, so we should show most-wished books instead
        worklist = models.Work.objects.order_by('-num_wishes')[:max]
    else:
        worklist = []
        if max > count:
            # add all the works with active campaigns
            for campaign in ending:
                worklist.append(campaign.work)
                
            # then fill out the rest of the list with popular but inactive works
            remainder = max - count
            remainder_works = models.Work.objects.exclude(campaigns__status='ACTIVE').order_by('-num_wishes')[:remainder]
            worklist.extend(remainder_works)
        else:
            # if the active campaign list has more works than we can fit 
            # in our slideshow, it's the only source we need to draw from
            while j < max:
                worklist.append(ending[j].work)
                j +=1
                
    return worklist

def next(request):
    if request.COOKIES.has_key('next'):
        response = HttpResponseRedirect(urllib.unquote(request.COOKIES['next']))
        response.delete_cookie('next')
        return response
    else:
        return HttpResponseRedirect('/')

def home(request):
    if request.user.is_authenticated():
        return HttpResponseRedirect(reverse('supporter',
            args=[request.user.username]))

    worklist = slideshow(12)
    works = worklist[:6]
    works2 = worklist[6:12]

    events = models.Wishes.objects.order_by('-created')[0:2]
    return render(request, 'home.html', {'suppress_search_box': True, 'works': works, 'works2': works2, 'events': events})

def stub(request):
    path = request.path[6:] # get rid of /stub/
    return render(request,'stub.html', {'path': path})

def work(request, work_id, action='display'):
    try:
        work = models.Work.objects.get(id = work_id)
    except models.Work.DoesNotExist:
        try:
            work = models.WasWork.objects.get(was = work_id).work
        except models.WasWork.DoesNotExist:
            raise Http404

    if request.method == 'POST' and not request.user.is_anonymous():
        activetab = '4'
        ebook_form= EbookForm( data = request.POST)
        if ebook_form.is_valid():
            ebook_form.save()
            alert = 'Thanks for adding an ebook to unglue.it!'
        else: 
            alert = ebook_form.errors
    else:
        alert=''
        try:
            activetab = request.GET['tab']
            if activetab not in ['1', '2', '3', '4']:
                activetab = '1';
        except:
            activetab = '1';
    editions = work.editions.all().order_by('-publication_date')
    campaign = work.last_campaign()
    if action == 'preview':
        work.last_campaign_status = 'ACTIVE'
    try:
        pledged = campaign.transactions().filter(user=request.user, status="ACTIVE")
    except:
        pledged = None

    try:
        pubdate = work.publication_date[:4]
    except IndexError:
        pubdate = 'unknown'
    if not request.user.is_anonymous():
        claimform = UserClaimForm( request.user, data={'claim-work':work.pk, 'claim-user': request.user.id}, prefix = 'claim')
        for edition in editions:
            #edition.ebook_form = EbookForm( data = {'user':request.user.id, 'edition':edition.pk })
            edition.ebook_form = EbookForm( instance= models.Ebook(user = request.user, edition = edition, provider = 'x' ), prefix = 'ebook_%d'%edition.id)
    else:
        claimform = None
    if campaign:
        # pull up premiums explicitly tied to the campaign or generic premiums
        premiums = campaign.effective_premiums()
    else:
        premiums = None
        
    wishers = work.num_wishes
    base_url = request.build_absolute_uri("/")[:-1]
    
    active_claims = work.claim.all().filter(status='active')
    if active_claims.count() == 1:
        claimstatus = 'one_active'
        rights_holder_name = active_claims[0].rights_holder.rights_holder_name
    else:
        rights_holder_name = None
        pending_claims = work.claim.all().filter(status='pending')
        pending_claims_count = pending_claims.count()
        if pending_claims_count > 1:
          claimstatus = 'disputed'
        elif pending_claims_count == 1:
          claimstatus = 'one_pending'
          rights_holder_name = pending_claims[0].rights_holder.rights_holder_name
        else:
          claimstatus = 'open'
    
    return render(request, 'work.html', {
        'work': work, 
        'premiums': premiums, 
        'ungluers': userlists.supporting_users(work, 5), 
        'claimform': claimform,
        'wishers': wishers,
        'base_url': base_url,
        'editions': editions,
        'pubdate': pubdate,
        'pledged': pledged,
        'activetab': activetab,
        'alert': alert,
        'claimstatus': claimstatus,
        'rights_holder_name': rights_holder_name,
    })

def manage_campaign(request, id):
    campaign = get_object_or_404(models.Campaign, id=id)
    campaign.not_manager=False
    campaign.problems=[]
    if (not request.user.is_authenticated) or (not request.user in campaign.managers.all()):
        campaign.not_manager=True
        return render(request, 'manage_campaign.html', {'campaign': campaign})
    alerts = []   
    if request.method == 'POST' :
        if request.POST.has_key('add_premium') :
            postcopy=request.POST.copy()
            postcopy['type']='CU'
            new_premium_form = CustomPremiumForm(data=postcopy)
            if new_premium_form.is_valid():
                new_premium_form.save()
                alerts.append(_('New premium has been added'))
                new_premium_form = CustomPremiumForm(data={'campaign': campaign})
            else:
                alerts.append(_('New premium has not been added'))              
            form = ManageCampaignForm(instance=campaign)
        elif request.POST.has_key('save') or  request.POST.has_key('launch') :
            form= ManageCampaignForm(instance=campaign, data=request.POST)  
            if form.is_valid():     
                form.save() 
                alerts.append(_('Campaign data has been saved'))
            else:
                alerts.append(_('Campaign data has NOT been saved'))
            if 'launch' in request.POST.keys():
                if campaign.launchable and form.is_valid() :
                    campaign.activate()
                    alerts.append(_('Campaign has been launched'))
                else:
                    alerts.append(_('Campaign has NOT been launched'))
            new_premium_form = CustomPremiumForm(data={'campaign': campaign})
        elif request.POST.has_key('inactivate') :
            if request.POST.has_key('premium_id'):
                premiums_to_stop = request.POST['premium_id']
                for premium_to_stop in premiums_to_stop:
                    selected_premium = models.Premium.objects.get(id=premium_to_stop)
                    if selected_premium.type == 'CU':
                        selected_premium.type = 'XX'
                        selected_premium.save()
                        alerts.append(_('Premium %s has been inactivated'% premium_to_stop))   
            form = ManageCampaignForm(instance=campaign)
            new_premium_form = CustomPremiumForm(data={'campaign': campaign})
    else:
        form = ManageCampaignForm(instance=campaign)
        new_premium_form = CustomPremiumForm(data={'campaign': campaign})
        
    return render(request, 'manage_campaign.html', {
        'campaign': campaign, 
        'form':form, 
        'problems': campaign.problems, 
        'alerts': alerts, 
        'premiums' : campaign.effective_premiums(),
        'premium_form' : new_premium_form,
    })
        
def googlebooks(request, googlebooks_id):
    try: 
        edition = models.Identifier.objects.get(type='goog',value=googlebooks_id).edition
    except models.Identifier.DoesNotExist:
        try:
            edition = bookloader.add_by_googlebooks_id(googlebooks_id)
            if edition.new:
                # add related editions asynchronously
                tasks.populate_edition.delay(edition.isbn_13)
        except bookloader.LookupFailure:
            logger.warning("failed to load googlebooks_id %s" % googlebooks_id)
            return HttpResponseNotFound("failed looking up googlebooks id %s" % googlebooks_id)
    if not edition:
        return HttpResponseNotFound("invalid googlebooks id")
    work_url = reverse('work', kwargs={'work_id': edition.work.id})
    return HttpResponseRedirect(work_url)

def subjects(request):
    order = request.GET.get('order')
    subjects = models.Subject.objects.all()
    subjects = subjects.annotate(Count('works'))

    if request.GET.get('order') == 'count':
        subjects = subjects.order_by('-works__count')
    else:
        subjects = subjects.order_by('name')

    return render(request, 'subjects.html', {'subjects': subjects})


recommended_user = User.objects.filter( username=settings.UNGLUEIT_RECOMMENDED_USERNAME)

class WorkListView(ListView):
    template_name = "work_list.html"
    context_object_name = "work_list"
    
    def get_queryset(self):
        facet = self.kwargs['facet']
        if (facet == 'popular'):
            return models.Work.objects.order_by('-num_wishes', 'id')
        elif (facet == 'recommended'):
            return models.Work.objects.filter(wishlists__user=recommended_user).order_by('-num_wishes')
        elif (facet == 'new'):
            return models.Work.objects.filter(num_wishes__gt=0).order_by('-created', '-num_wishes' ,'id')
        else:
            return models.Work.objects.all().order_by('-created', 'id')

    def get_context_data(self, **kwargs):
            context = super(WorkListView, self).get_context_data(**kwargs)
            qs=self.get_queryset()
            context['ungluers'] = userlists.work_list_users(qs,5)
            context['facet'] =self.kwargs['facet']
            context['works_unglued'] = qs.filter(editions__ebooks__isnull=False).distinct()
            context['works_active'] = qs.exclude(editions__ebooks__isnull=False).filter(Q(campaigns__status='ACTIVE') | Q(campaigns__status='SUCCESSFUL')).distinct()
            context['works_wished'] = qs.exclude(editions__ebooks__isnull=False).exclude(campaigns__status='ACTIVE').exclude(campaigns__status='SUCCESSFUL').distinct()
            
            context['activetab'] = "#3"
            
            counts={}
            counts['unglued'] = context['works_unglued'].count()
            counts['unglueing'] = context['works_active'].count()
            counts['wished'] = context['works_wished'].count()
            context['counts'] = counts
            return context

class UngluedListView(ListView):
    template_name = "unglued_list.html"
    context_object_name = "work_list"
    
    def work_set_counts(self,work_set):
        counts={}
        counts['unglued'] = work_set.annotate(ebook_count=Count('editions__ebooks')).filter(ebook_count__gt=0).count()
        return counts

    def get_queryset(self):
        facet = self.kwargs['facet']
        if (facet == 'popular'):
            return models.Work.objects.filter(editions__ebooks__isnull=False).distinct().order_by('-num_wishes')
        else:
            #return models.Work.objects.annotate(ebook_count=Count('editions__ebooks')).filter(ebook_count__gt=0).order_by('-created')
            return models.Work.objects.filter(editions__ebooks__isnull=False).distinct().order_by('-created')

    def get_context_data(self, **kwargs):
            context = super(UngluedListView, self).get_context_data(**kwargs)
            qs=self.get_queryset()
            context['counts'] = self.work_set_counts(qs)
            context['ungluers'] = userlists.work_list_users(qs,5)
            context['facet'] =self.kwargs['facet']
            context['activetab'] = "#1"
            return context

        
class CampaignListView(ListView):
    template_name = "campaign_list.html"
    context_object_name = "campaign_list"
    model = models.Campaign

    def get_queryset(self):
        facet = self.kwargs['facet']
        if (facet == 'newest'):
            return models.Campaign.objects.filter(status='ACTIVE').order_by('-activated')
        elif (facet == 'pledged'):
            return models.Campaign.objects.filter(status='ACTIVE').annotate(total_pledge=Sum('transaction__amount')).order_by('-total_pledge')
        elif (facet == 'pledges'):
            return models.Campaign.objects.filter(status='ACTIVE').annotate(pledges=Count('transaction')).order_by('-pledges')
        elif (facet == 'almost'):
            return models.Campaign.objects.filter(status='ACTIVE').all() # STUB: will need to make db changes to make this work 
        elif (facet == 'ending'):
            return models.Campaign.objects.filter(status='ACTIVE').order_by('deadline')
        else:
            return models.Campaign.objects.all()

    def get_context_data(self, **kwargs):
            context = super(CampaignListView, self).get_context_data(**kwargs)
            qs=self.get_queryset()
            context['ungluers'] = userlists.campaign_list_users(qs,5)
            context['facet'] =self.kwargs['facet']
            return context
            
class PledgeView(FormView):
    template_name="pledge.html"
    form_class = CampaignPledgeForm
    embedded = False
    
    def get(self, request, *args, **kwargs):
    # change the default behavior from https://code.djangoproject.com/browser/django/tags/releases/1.3.1/django/views/generic/edit.py#L129
    # don't automatically bind the data to the form on GET, only on POST
    # compare with https://code.djangoproject.com/browser/django/tags/releases/1.3.1/django/views/generic/edit.py#L34
        form_class = self.get_form_class()
        form = form_class()
        
        return self.render_to_response(self.get_context_data(form=form))    
    
    def get_context_data(self, **kwargs):
        context = super(PledgeView, self).get_context_data(**kwargs)
        
        work = get_object_or_404(models.Work, id=self.kwargs["work_id"])
        
        campaign = work.last_campaign()
        
        if campaign:
            premiums = campaign.effective_premiums()
                
        premium_id = self.request.REQUEST.get('premium_id', None)
        preapproval_amount = self.request.REQUEST.get('preapproval_amount', None)
        
        if premium_id is not None and preapproval_amount is None:
            try:
                preapproval_amount = D(models.Premium.objects.get(id=premium_id).amount)
            except:
                preapproval_amount = None
              
        data = {'preapproval_amount':preapproval_amount, 'premium_id':premium_id}
        
        form_class = self.get_form_class()
        
        # no validation errors, please, when we're only doing a GET
        # to avoid validation errors, don't bind the form

        if preapproval_amount is not None:
            form = form_class(data)
        else:
            form = form_class()
    
        context.update({'work':work,'campaign':campaign, 'premiums':premiums, 'form':form, 'premium_id':premium_id, 'faqmenu': 'pledge'})
        return context
    
    def form_valid(self, form):
        work_id = self.kwargs["work_id"]
        preapproval_amount = form.cleaned_data["preapproval_amount"]
        anonymous = form.cleaned_data["anonymous"]
        
        # right now, if there is a non-zero pledge amount, go with that. otherwise, do the pre_approval
        campaign = models.Work.objects.get(id=int(work_id)).last_campaign()
        
        premium_id = form.cleaned_data["premium_id"]
        # confirm that the premium_id is a valid one for the campaign in question
        try:
            premium = models.Premium.objects.get(id=premium_id)
            if not (premium.campaign is None or premium.campaign == campaign):
                 premium = None
        except models.Premium.DoesNotExist, e:
            premium = None
        
        p = PaymentManager(embedded=self.embedded)
                    
        # PledgeView is wrapped in login_required -- so in theory, user should never be None -- but I'll keep this logic here for now.
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = None
                   
        if not self.embedded:
            
            return_url = None
            cancel_url = None
            
            # the recipients of this authorization is not specified here but rather by the PaymentManager.
            # set the expiry date based on the campaign deadline
            expiry = campaign.deadline + timedelta( days=settings.PREAPPROVAL_PERIOD_AFTER_CAMPAIGN )
            t, url = p.authorize('USD', TARGET_TYPE_CAMPAIGN, preapproval_amount, expiry=expiry, campaign=campaign, list=None, user=user,
                            return_url=return_url, cancel_url=cancel_url, anonymous=anonymous, premium=premium)    
        else:  # embedded view -- which we're not actively using right now.
            # embedded view triggerws instant payment:  send to the partnering RH
            receiver_list = [{'email':settings.PAYPAL_NONPROFIT_PARTNER_EMAIL, 'amount':preapproval_amount}]
            
            return_url = None
            cancel_url = None
            
            t, url = p.pledge('USD', TARGET_TYPE_CAMPAIGN, receiver_list, campaign=campaign, list=None, user=user,
                              return_url=return_url, cancel_url=cancel_url, anonymous=anonymous, premium=premium)
        
        if url:
            logger.info("PledgeView paypal: " + url)
            print >> sys.stderr, "CampaignFormView paypal: ", url
            return HttpResponseRedirect(url)
        else:
            response = t.reference
            logger.info("PledgeView paypal: Error " + str(t.reference))
            return HttpResponse(response)

class PledgeModifyView(FormView):
    """
    A view to handle request to change an existing pledge
    """
    template_name="pledge.html"
    form_class = CampaignPledgeForm
    embedded = False

    def get_context_data(self, **kwargs):
        
        context = super(PledgeModifyView, self).get_context_data(**kwargs)
        
        # the following should be true since PledgeModifyView.as_view is wrapped in login_required
        assert self.request.user.is_authenticated()
        user = self.request.user
        
        work = get_object_or_404(models.Work, id=self.kwargs["work_id"])
        
        try:
            campaign = work.last_campaign()
            premiums = campaign.effective_premiums()
            
            # which combination of campaign and transaction status required?
            # Campaign must be ACTIVE
            assert campaign.status == 'ACTIVE'

            transactions = campaign.transactions().filter(user=user, status=TRANSACTION_STATUS_ACTIVE)
            assert transactions.count() == 1
            transaction = transactions[0]
            assert transaction.type == PAYMENT_TYPE_AUTHORIZATION and transaction.status == TRANSACTION_STATUS_ACTIVE
           
        except Exception, e:
            raise e
        
        # what stuff do we need to pull out to populate form?
        # preapproval_amount, premium_id (which we don't have stored yet)
        if transaction.premium is not None:
            premium_id = transaction.premium.id
        else:
            premium_id = None
        
        # is there a Transaction for an ACTIVE campaign for this
        # should make sure Transaction is modifiable.
        
        preapproval_amount = transaction.amount      
        data = {'preapproval_amount':preapproval_amount, 'premium_id':premium_id}
        
        # initialize form with the current state of the transaction if the current values empty
        form = kwargs['form']
        
        if not(form.is_bound):
            form_class = self.get_form_class()
            form = form_class(initial=data)
    
        context.update({'work':work,'campaign':campaign, 'premiums':premiums, 'form':form,'preapproval_amount':preapproval_amount, 'premium_id':premium_id, 'faqmenu': 'modify'})
        return context
    
    
    def form_invalid(self, form):
        logger.info("form.non_field_errors: {0}".format(form.non_field_errors()))
        response =  self.render_to_response(self.get_context_data(form=form))
        return response
        
    def form_valid(self, form):
        
        # What are the situations we need to deal with?
        # 2 main situations:  if the new amount is less than max_amount, no need to go out to PayPal again
        # if new amount is greater than max_amount...need to go out and get new approval.
        # to start with, we can use the standard pledge_complete, pledge_cancel machinery
        # might have to modify the pledge_complete, pledge_cancel because the messages are going to be
        # different because we're modifying a pledge rather than a new one.
        
        work_id = self.kwargs["work_id"]
        preapproval_amount = form.cleaned_data["preapproval_amount"]
        anonymous = form.cleaned_data["anonymous"]
 
        assert self.request.user.is_authenticated()
        user = self.request.user       
                
        # right now, if there is a non-zero pledge amount, go with that.  otherwise, do the pre_approval
        campaign = models.Work.objects.get(id=int(work_id)).last_campaign()
        assert campaign.status == 'ACTIVE'
    
        premium_id = form.cleaned_data["premium_id"]
        # confirm that the premium_id is a valid one for the campaign in question
        try:
            premium = models.Premium.objects.get(id=premium_id)
            if not (premium.campaign is None or premium.campaign == campaign):
                 premium = None
        except models.Premium.DoesNotExist, e:
            premium = None
    
        transactions = campaign.transactions().filter(user=user, status=TRANSACTION_STATUS_ACTIVE)
        assert transactions.count() == 1
        transaction = transactions[0]
        assert transaction.type == PAYMENT_TYPE_AUTHORIZATION and transaction.status == TRANSACTION_STATUS_ACTIVE        
        
        p = PaymentManager(embedded=self.embedded)
        status, url = p.modify_transaction(transaction=transaction, amount=preapproval_amount, premium=premium)
        
        logger.info("status: {0}, url:{1}".format(status, url))
        
        if status and url is not None:
            logger.info("PledgeModifyView paypal: " + url)
            return HttpResponseRedirect(url)
        elif status and url is None:
            # let's use the pledge_complete template for now and maybe look into customizing it.
            return HttpResponseRedirect("{0}?tid={1}".format(reverse('pledge_complete'), transaction.id))
        else:
            return HttpResponse("No modication made")


class PledgeCompleteView(TemplateView):
    """A callback for PayPal to tell unglue.it that a payment transaction has completed successfully.
    
    Possible things to implement:
    
        after pledging, supporter receives email including thanks, work pledged, amount, expiry date, any next steps they should expect; others?
    study other confirmation emails for their contents
    after pledging, supporters are returned to a thank-you screen
    should have prominent "thank you" or "congratulations" message
    should have prominent share options
    should suggest other works for supporters to explore (on what basis?)
    link to work page? or to page on which supporter entered the process? (if the latter, how does that work with widgets?)
    should note that a confirmation email has been sent to $email from $sender
    should briefly note next steps (e.g. if this campaign succeeds you will be emailed on date X)    
        
    """
    
    template_name="pledge_complete.html"
    
    def get_context_data(self):
        # pick up all get and post parameters and display
        context = super(PledgeCompleteView, self).get_context_data()

        output = "pledge complete"
        output += self.request.method + "\n" + str(self.request.REQUEST.items())
        context["output"] = output
        
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = None
        
        # pull out the transaction id and try to get the corresponding Transaction
        transaction_id = self.request.REQUEST.get("tid")
        transaction = Transaction.objects.get(id=transaction_id)
        
        # work and campaign in question
        try:
            campaign = transaction.campaign
            work = campaign.work
        except Exception, e:
            campaign = None
            work = None
        
        # we need to check whether the user tied to the transaction is indeed the authenticated user.
        
        correct_user = False 
        try:
            if user.id == transaction.user.id:
                correct_user = True
            else:
                # should be 403 -- but let's try 404 for now -- 403 exception coming in Django 1.4
                raise Http404
        except Exception, e:
            raise Http404
            
            
        # check that the user had not already approved the transaction
        # do we need to first run PreapprovalDetails to check on the status
        
        # is it of type=PAYMENT_TYPE_AUTHORIZATION and status is NONE or ACTIVE (but approved is false)
        
        if transaction.type == PAYMENT_TYPE_AUTHORIZATION:
            correct_transaction_type = True
        else:
            correct_transaction_type = False
            
        # add the work corresponding to the Transaction on the user's wishlist if it's not already on the wishlist
        if user is not None and correct_user and correct_transaction_type and (campaign is not None) and (work is not None):
            # ok to overwrite Wishes.source?
            user.wishlist.add_work(work, 'pledging')
            
        worklist = slideshow(12)
        works = worklist[:6]
        works2 = worklist[6:12]

        context["transaction"] = transaction
        context["correct_user"] = correct_user
        context["correct_transaction_type"] = correct_transaction_type
        context["work"] = work
        context["campaign"] = campaign
        context["faqmenu"] = "complete"
        context["works"] = works
        context["works2"] = works2        
        
        return context        
                
    
class PledgeCancelView(TemplateView):
    """A callback for PayPal to tell unglue.it that a payment transaction has been canceled by the user"""
    template_name="pledge_cancel.html"
    
    def get_context_data(self):
        context = super(PledgeCancelView, self).get_context_data()
        
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = None
        
        # pull out the transaction id and try to get the corresponding Transaction
        transaction_id = self.request.REQUEST.get("tid")
        transaction = Transaction.objects.get(id=transaction_id)
        
        # work and campaign in question
        try:
            campaign = transaction.campaign
            work = campaign.work
        except Exception, e:
            campaign = None
            work = None
        
        # we need to check whether the user tied to the transaction is indeed the authenticated user.
        
        correct_user = False 
        try:
            if user.id == transaction.user.id:
                correct_user = True
        except Exception, e:
            pass
            
        # check that the user had not already approved the transaction
        # do we need to first run PreapprovalDetails to check on the status
        
        # is it of type=PAYMENT_TYPE_AUTHORIZATION and status is NONE or ACTIVE (but approved is false)
        
        if transaction.type == PAYMENT_TYPE_AUTHORIZATION:
            correct_transaction_type = True
        else:
            correct_transaction_type = False
            
        # status?

        # give the user an opportunity to approved the transaction again
        # provide a URL to click on.
        # https://www.sandbox.paypal.com/?cmd=_ap-preapproval&preapprovalkey=PA-6JV656290V840615H
        try_again_url = '%s?cmd=_ap-preapproval&preapprovalkey=%s' % (settings.PAYPAL_PAYMENT_HOST, transaction.preapproval_key)
        
        context["transaction"] = transaction
        context["correct_user"] = correct_user
        context["correct_transaction_type"] = correct_transaction_type
        context["try_again_url"] = try_again_url
        context["work"] = work
        context["campaign"] = campaign
        context["faqmenu"] = "cancel"
        
        return context
    
    
class DonateView(FormView):
    template_name="donate.html"
    form_class = DonateForm
    embedded = False
    
    #def get_context_data(self, **kwargs):
    #    context = super(DonateView, self).get_context_data(**kwargs)
    #    
    #    form = CampaignPledgeForm(data)
    #
    #    context.update({'work':work,'campaign':campaign, 'premiums':premiums, 'form':form, 'premium_id':premium_id})
    #    return context
    
    def form_valid(self, form):
        donation_amount = form.cleaned_data["donation_amount"]
        anonymous = form.cleaned_data["anonymous"]
        
        # right now, if there is a non-zero pledge amount, go with that.  otherwise, do the pre_approval
        campaign = None
        
        p = PaymentManager(embedded=self.embedded)
                    
        # we should force login at this point -- or if no account, account creation, login, and return to this spot
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = None

        # instant payment:  send to the partnering RH
        receiver_list = [{'email':settings.PAYPAL_NONPROFIT_PARTNER_EMAIL, 'amount':donation_amount}]
        
        #redirect the page back to campaign page on success
        return_url = self.request.build_absolute_uri(reverse('donate'))
        
        t, url = p.pledge('USD', TARGET_TYPE_DONATION, receiver_list, campaign=campaign, list=None, user=user,
                          return_url=return_url, anonymous=anonymous)
    
        if url:
            return HttpResponseRedirect(url)
        else:
            response = t.reference
            logger.info("PledgeView paypal: Error " + str(t.reference))
            return HttpResponse(response)
    
    
def claim(request):
    if  request.method == 'GET': 
        data = request.GET
    else:
        data =  request.POST
    form =  UserClaimForm(request.user, data=data, prefix='claim')
    if form.is_valid():
        # make sure we're not creating a duplicate claim
        if not models.Claim.objects.filter(work=data['claim-work'], rights_holder=data['claim-rights_holder'], status='pending').count():
            form.save()
        return HttpResponseRedirect(reverse('work', kwargs={'work_id': data['claim-work']}))
    else:
        work = models.Work.objects.get(id=data['claim-work'])
        rights_holder = models.RightsHolder.objects.get(id=data['claim-rights_holder'])
        active_claims = work.claim.exclude(status = 'release')
        context = {'form': form, 'work': work, 'rights_holder':rights_holder , 'active_claims':active_claims}
        return render(request, "claim.html", context)

def rh_tools(request):
    if not request.user.is_authenticated() :
        return render(request, "rh_tools.html")
    claims = request.user.claim.filter(user=request.user)
    campaign_form = "xxx"
    if not claims:
        return render(request, "rh_tools.html")
    for claim in claims:
        if claim.status == 'active':
            claim.campaigns = claim.work.campaigns.all()
        else:
            claim.campaigns = []
        claim.can_open_new=True
        for campaign in claim.campaigns:
            if campaign.status in ['ACTIVE','INITIALIZED']:
                claim.can_open_new=False
                if request.method == 'POST' and request.POST.has_key('edit_managers_%s'% campaign.id) :
                    campaign.edit_managers_form=EditManagersForm( instance=campaign, data=request.POST, prefix=campaign.id)
                    if campaign.edit_managers_form.is_valid():
                        campaign.edit_managers_form.save()
                        campaign.edit_managers_form = EditManagersForm(instance=campaign, prefix=campaign.id)
                else:
                    campaign.edit_managers_form=EditManagersForm(instance=campaign, prefix=campaign.id)
        if claim.status == 'active' and claim.can_open_new:
            if request.method == 'POST' and  request.POST.has_key('work') and int(request.POST['work']) == claim.work.id :
                claim.campaign_form = OpenCampaignForm(request.POST)
                if claim.campaign_form.is_valid():                    
                    new_campaign = claim.campaign_form.save(commit=False)
                    new_campaign.deadline = date_today() + timedelta(days=int(settings.UNGLUEIT_LONGEST_DEADLINE))
                    new_campaign.target = D(settings.UNGLUEIT_MINIMUM_TARGET)
                    new_campaign.save()
                    claim.campaign_form.save_m2m()
                    claim.can_open_new=False
            else:
                claim.campaign_form = OpenCampaignForm(data={'work': claim.work, 'name': claim.work.title, 'userid': request.user.id})
        else:
            claim.can_open_new=False
    return render(request, "rh_tools.html", {'claims': claims ,}) 

def rh_admin(request):
    if not request.user.is_authenticated() :
        return render(request, "admins_only.html")
    if not request.user.is_staff :
        return render(request, "admins_only.html")
    PendingFormSet = modelformset_factory(models.Claim, fields=['status'], extra=0)
    pending_data = models.Claim.objects.filter(status = 'pending')
    active_data = models.Claim.objects.filter(status = 'active')
    if  request.method == 'POST': 
        if 'create_rights_holder' in request.POST.keys():
            form = RightsHolderForm(data=request.POST)
            pending_formset = PendingFormSet (queryset=pending_data)
            if form.is_valid():
                form.save()
                form = RightsHolderForm()
        if 'set_claim_status' in request.POST.keys():
            pending_formset = PendingFormSet (request.POST, request.FILES, queryset=pending_data)
            form = RightsHolderForm()
            if pending_formset.is_valid():
                pending_formset.save()
                pending_formset = PendingFormSet(queryset=pending_data)
    else:
        form = RightsHolderForm()
        pending_formset = PendingFormSet(queryset=pending_data)
    rights_holders = models.RightsHolder.objects.all()
    
    context = { 
        'request': request, 
        'rights_holders': rights_holders, 
        'form': form,
        'pending': zip(pending_data,pending_formset),
        'pending_formset': pending_formset,
        'active_data': active_data,
    }
    return render(request, "rights_holders.html", context)

def campaign_admin(request):
    if not request.user.is_authenticated() :
        return render(request, "admins_only.html")    
    if not request.user.is_staff :
        return render(request, "admins_only.html")
        
    context = {}
    
    def campaigns_types():
        # pull out Campaigns with Transactions that are ACTIVE -- and hence can be executed
        # Campaign.objects.filter(transaction__status='ACTIVE')
        
        campaigns_with_active_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_ACTIVE)
            
        # pull out Campaigns with Transactions that are INCOMPLETE
    
        campaigns_with_incomplete_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_INCOMPLETE)
        
        # show all Campaigns with Transactions that are COMPLETED
    
        campaigns_with_completed_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_COMPLETE_PRIMARY)
        
        # show Campaigns with Transactions that are CANCELED
        
        campaigns_with_canceled_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_CANCELED)
        
        return (campaigns_with_active_transactions, campaigns_with_incomplete_transactions, campaigns_with_completed_transactions, campaigns_with_canceled_transactions)
        
    form = CampaignAdminForm()
    pm = PaymentManager()
    check_status_results = None
    command_status = None
    
    if request.method == 'GET':
        pass
    elif request.method == 'POST':
        if 'campaign_checkstatus' in request.POST.keys():
            # campaign_checkstatus
            try:
                status = pm.checkStatus()
                check_status_results = ""
                # parse the output to display chat transaction statuses have been updated
                if len(status["preapprovals"]):
                    for t in status["preapprovals"]:
                        check_status_results += "<p>Preapproval key: %s updated</p>" % (t["key"])
                else:
                    check_status_results += "<p>No preapprovals needed updating</p>"
                if len(status["payments"]):
                    for t in status["payments"]:
                        info = ", ".join(["%s:%s" % (k,v) for (k,v) in t.items()])
                        check_status_results += "<p>Payment updated: %s </p>" % (info)
                        
                else:
                    check_status_results += "<p>No payments needed updating</p>"                    
                command_status = _("Transactions updated based on PaymentDetails and PreapprovalDetails")
            except Exception, e:
                check_status_results = e
        elif 'execute_campaigns' in request.POST.keys():            
            c_id = request.POST.get('active_campaign', None)
            if c_id is not None:
                try:
                    campaign = models.Campaign.objects.get(id=c_id)
                    results = pm.execute_campaign(campaign)
                    command_status = str(results)
                except Exception, e:
                    command_status = "Error in executing transactions for campaign %s " % (str(e))
        elif 'finish_campaigns' in request.POST.keys():
            c_id = request.POST.get('incomplete_campaign', None)
            if c_id is not None:
                try:
                    campaign = models.Campaign.objects.get(id=c_id)
                    results = pm.finish_campaign(campaign)
                    command_status = str(results)
                except Exception, e:
                    command_status = "Error in finishing transactions for campaign %s " % (str(e))            
            
        elif 'cancel_campaigns' in request.POST.keys():
            c_id = request.POST.get('active_campaign', None)
            if c_id is not None:
                try:
                    campaign = models.Campaign.objects.get(id=c_id)
                    results = pm.cancel_campaign(campaign)
                    command_status = str(results)
                except Exception, e:
                    command_status = "Error in canceling transactions for campaign %s " % (str(e))        
            
    (campaigns_with_active_transactions, campaigns_with_incomplete_transactions, campaigns_with_completed_transactions,
                campaigns_with_canceled_transactions) = campaigns_types()
    
    context.update({
        'form': form,
        'check_status_results':check_status_results,
        'campaigns_with_active_transactions': campaigns_with_active_transactions,
        'campaigns_with_incomplete_transactions': campaigns_with_incomplete_transactions,
        'campaigns_with_completed_transactions': campaigns_with_completed_transactions,
        'campaigns_with_canceled_transactions': campaigns_with_canceled_transactions,
        'command_status': command_status
    })

    return render(request, "campaign_admin.html", context)

def supporter(request, supporter_username, template_name):
    supporter = get_object_or_404(User, username=supporter_username)
    wishlist = supporter.wishlist
    works = []
    works2 = []
    works_unglued = []
    works_active = []
    works_wished = []
    is_preview = settings.IS_PREVIEW
    
    if (wishlist.works.all()):
        # querysets for tabs
        # unglued tab is anything with an existing ebook
        ## .order_by() may clash with .distinct() and this should be fixed
        works_unglued = wishlist.works.all().filter(editions__ebooks__isnull=False).distinct().order_by('-num_wishes')
        
        # take the set complement of the unglued tab and filter it for active works to get middle tab
        result = wishlist.works.all().exclude(pk__in=works_unglued.values_list('pk', flat=True))
        works_active = result.filter(Q(campaigns__status='ACTIVE') | Q(campaigns__status='SUCCESSFUL')).order_by('-campaigns__status', 'campaigns__deadline').distinct()
        
        # everything else goes in tab 3
        works_wished = result.exclude(pk__in=works_active.values_list('pk', flat=True)).order_by('-num_wishes')
        
        # badge counts
        backed = works_unglued.count()
        backing = works_active.count()
        wished = works_wished.count()
    
    else:           
        backed = 0
        backing = 0
        wished = 0
        
        worklist = slideshow(8)
        works = worklist[:4]
        works2 = worklist[4:8]
        
	# default to showing the Active tab if there are active campaigns, else show Wishlist
    if backing > 0:
    	activetab = "#2"
    else:
    	activetab = "#3"
    
    date = supporter.date_joined.strftime("%B %d, %Y")
    
    # following block to support profile admin form in supporter page
    if request.user.is_authenticated() and request.user.username == supporter_username:

        try:
            profile_obj=request.user.get_profile()
        except ObjectDoesNotExist:
            profile_obj= models.UserProfile()
            profile_obj.user=request.user

        if  request.method == 'POST': 
            profile_form = ProfileForm(data=request.POST,instance=profile_obj)
            if profile_form.is_valid():
                if profile_form.cleaned_data['clear_facebook'] or profile_form.cleaned_data['clear_twitter'] or  profile_form.cleaned_data['clear_goodreads'] :
                    if profile_form.cleaned_data['clear_facebook']:
                        profile_obj.facebook_id=0
                    if profile_form.cleaned_data['clear_twitter']:
                        profile_obj.twitter_id=""
                    if profile_form.cleaned_data['clear_goodreads']:
                        profile_obj.goodreads_user_id = None
                        profile_obj.goodreads_user_name = None
                        profile_obj.goodreads_user_link = None
                        profile_obj.goodreads_auth_token = None
                        profile_obj.goodreads_auth_secret = None

                    profile_obj.save()
                profile_form.save()

        else:
            profile_form= ProfileForm(instance=profile_obj)
        
        if request.user.profile.goodreads_user_id is not None:
            goodreads_id = request.user.profile.goodreads_user_id
        else:
            goodreads_id = None

        if request.user.profile.librarything_id is not None:
            librarything_id = request.user.profile.librarything_id
        else:
            librarything_id = None
    else:
        profile_form = ''
        goodreads_id = None
        librarything_id = None

    context = {
            "supporter": supporter,
            "wishlist": wishlist,
            "works_unglued": works_unglued,
            "works_active": works_active,
            "works_wished": works_wished,
            "works": works,
            "works2": works2,
            "is_preview": is_preview,
            "backed": backed,
            "backing": backing,
            "wished": wished,
            "date": date,
            "profile_form": profile_form,
            "ungluers": userlists.other_users(supporter, 5 ),
            "goodreads_auth_url": reverse('goodreads_auth'),
            "goodreads_id": goodreads_id,
            "librarything_id": librarything_id,
            "activetab": activetab
    }
    
    return render(request, template_name, context)

def edit_user(request):
    if not request.user.is_authenticated():
        return HttpResponseRedirect(reverse('auth_login'))    
    form=UserData()
    emailform = UserEmail({'email':request.user.email})
    oldusername=request.user.username
    oldemail= request.user.email
    if request.method == 'POST': 
        if 'change_username' in request.POST.keys():
            # surely there's a better way to add data to the POST data?
            postcopy=request.POST.copy()
            postcopy['oldusername']=oldusername 
            form = UserData(postcopy)
            if form.is_valid(): # All validation rules pass, go and change the username
                request.user.username=form.cleaned_data['username']
                request.user.save()
                return HttpResponseRedirect(reverse('home')) # Redirect after POST
        elif 'change_email'  in request.POST.keys():
            emailform = UserEmail(request.POST)
            if emailform.is_valid():
                request.user.email=emailform.cleaned_data['email']
                request.user.save()
                send_mail_task.delay(
                    'unglue.it email changed', 
                    render_to_string('registration/email_changed.txt',{'oldemail':oldemail,'request':request}),
                    None,
                    [request.user.email,oldemail]
                    )
                return HttpResponseRedirect(reverse('home')) # Redirect after POST
    return render(request,'registration/user_change_form.html', {'form': form,'emailform': emailform})  


def search(request):
    q = request.GET.get('q', None)
    page = int(request.GET.get('page', 1))
    results = gluejar_search(q, user_ip=request.META['REMOTE_ADDR'], page=page)

    # flag search result as on wishlist as appropriate
    if not request.user.is_anonymous():
        ungluers = userlists.other_users(request.user, 5)
    else:
        ungluers = userlists.other_users(None, 5)

    works=[]
    for result in results:
        try:
            work = models.Identifier.objects.get(type='goog',value=result['googlebooks_id']).work
            works.append(work)
        except models.Identifier.DoesNotExist: 
            works.append(result)
    context = {
        "q": q,
        "results": works,
        "ungluers": ungluers
    }
    return render(request, 'search.html', context)

# TODO: perhaps this functionality belongs in the API?
@require_POST
@login_required
@csrf_exempt
def wishlist(request):
    googlebooks_id = request.POST.get('googlebooks_id', None)
    remove_work_id = request.POST.get('remove_work_id', None)
    add_work_id = request.POST.get('add_work_id', None)

    if googlebooks_id:
        try:
            edition = bookloader.add_by_googlebooks_id(googlebooks_id)
            if edition.new:
                # add related editions asynchronously
                tasks.populate_edition.delay(edition.isbn_13)
            request.user.wishlist.add_work(edition.work,'user')
        except bookloader.LookupFailure:
            logger.warning("failed to load googlebooks_id %s" % googlebooks_id)
        except Exception, e:
            logger.warning("Error in wishlist adding %s" % (e))          
        # TODO: redirect to work page, when it exists
        return HttpResponseRedirect('/')
    elif remove_work_id:
        try:
            work = models.Work.objects.get(id=int(remove_work_id))
        except models.Work.DoesNotExist:
            try:
                work = models.WasWork.objects.get(was = work_id).work
            except models.WasWork.DoesNotExist:
                raise Http404
        request.user.wishlist.remove_work(work)
        # TODO: where to redirect?
        return HttpResponseRedirect('/')
    elif add_work_id:
        # if adding from work page, we have may work.id, not googlebooks_id
        try:
            work = models.Work.objects.get(pk=add_work_id)
        except models.Work.DoesNotExist:
            try:
                work = models.WasWork.objects.get(was = work_id).work
            except models.WasWork.DoesNotExist:
                raise Http404

        request.user.wishlist.add_work(work,'user')
        return HttpResponseRedirect('/')
  
class CampaignFormView(FormView):
    template_name="campaign_detail.html"
    form_class = CampaignPledgeForm
    embedded = False
    
    def get_context_data(self, **kwargs):
        pk = self.kwargs["pk"]
        campaign = models.Campaign.objects.get(id=int(pk))
        context = super(CampaignFormView, self).get_context_data(**kwargs)
        base_url = self.request.build_absolute_uri("/")[:-1]
        context.update({
           'embedded': self.embedded,
           'campaign': campaign,
           'base_url':base_url
        })
        
        return context

    def form_valid(self,form):
        pk = self.kwargs["pk"]
        preapproval_amount = form.cleaned_data["preapproval_amount"]
        anonymous = form.cleaned_data["anonymous"]
        
        # right now, if there is a non-zero pledge amount, go with that.  otherwise, do the pre_approval
        campaign = models.Campaign.objects.get(id=int(pk))
        
        p = PaymentManager(embedded=self.embedded)
                    
        # we should force login at this point -- or if no account, account creation, login, and return to this spot
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = None
            
        # calculate the work corresponding to the campaign id
        work_id = campaign.work.id
        
        # set the expiry date based on the campaign deadline
        expiry = campaign.deadline + timedelta( days=settings.PREAPPROVAL_PERIOD_AFTER_CAMPAIGN )
        
        if not self.embedded:
            
            return_url = self.request.build_absolute_uri(reverse('work',kwargs={'work_id': str(work_id)}))
            t, url = p.authorize('USD', TARGET_TYPE_CAMPAIGN, preapproval_amount, expiry=expiry, campaign=campaign, list=None, user=user,
                            return_url=return_url, anonymous=anonymous)    
        else:
            # instant payment:  send to the partnering RH
            # right now, all money going to Gluejar.  
            receiver_list = [{'email':settings.PAYPAL_GLUEJAR_EMAIL, 'amount':preapproval_amount}]
            
            #redirect the page back to campaign page on success
            return_url = self.request.build_absolute_uri(reverse('campaign_by_id',kwargs={'pk': str(pk)}))
            t, url = p.pledge('USD', TARGET_TYPE_CAMPAIGN, receiver_list, campaign=campaign, list=None, user=user,
                              return_url=return_url, anonymous=anonymous)
        
        if url:
            logger.info("CampaignFormView paypal: " + url)
            print >> sys.stderr, "CampaignFormView paypal: ", url
            return HttpResponseRedirect(url)
        else:
            response = t.reference
            logger.info("CampaignFormView paypal: Error " + str(t.reference))
            return HttpResponse(response)

class InfoPageView(TemplateView):
    
    def get_template_names(self, **kwargs):
        if self.kwargs['template_name']:
            return (self.kwargs['template_name'])
        else:
            return ('metrics.html')
            
    def get_context_data(self, **kwargs):
        users = User.objects
        users.today = users.filter(date_joined__range = (date_today(), now()))
        users.days7 = users.filter(date_joined__range = (date_today()-timedelta(days=7), now()))
        users.year = users.filter(date_joined__year = date_today().year)
        users.month = users.year.filter(date_joined__month = date_today().month)
        users.yesterday = users.filter(date_joined__range = (date_today()-timedelta(days=1), date_today()))
        users.gr = users.filter(profile__goodreads_user_id__isnull = False)
        users.lt = users.exclude(profile__librarything_id = '')
        users.fb = users.filter(profile__facebook_id__isnull = False)
        users.tw = users.exclude(profile__twitter_id = '')
        works = models.Work.objects
        works.today = works.filter(created__range = (date_today(), now()))
        works.days7 = works.filter(created__range = (date_today()-timedelta(days=7), now()))
        works.year = works.filter(created__year = date_today().year)
        works.month = works.year.filter(created__month = date_today().month)
        works.yesterday = works.filter(created__range = (date_today()-timedelta(days=1), date_today()))
        works.wishedby2 = works.filter(num_wishes__gte = 2)
        works.wishedby20 = works.filter(num_wishes__gte = 20)
        works.wishedby5 = works.filter(num_wishes__gte = 5)
        works.wishedby50 = works.filter(num_wishes__gte = 50)
        works.wishedby10 = works.filter(num_wishes__gte = 10)
        works.wishedby100 = works.filter(num_wishes__gte = 100)
        ebooks = models.Ebook.objects
        ebooks.today = ebooks.filter(created__range = (date_today(), now()))
        ebooks.days7 = ebooks.filter(created__range = (date_today()-timedelta(days=7), now()))
        ebooks.year = ebooks.filter(created__year = date_today().year)
        ebooks.month = ebooks.year.filter(created__month = date_today().month)
        ebooks.yesterday = ebooks.filter(created__range = (date_today()-timedelta(days=1), date_today()))
        wishlists= models.Wishlist.objects.exclude(wishes__isnull=True)
        wishlists.today = wishlists.filter(created__range = (date_today(), now()))
        wishlists.days7 = wishlists.filter(created__range = (date_today()-timedelta(days=7), now()))
        wishlists.year = wishlists.filter(created__year = date_today().year)
        wishlists.month = wishlists.year.filter(created__month = date_today().month)
        if date_today().day==1:
            wishlists.yesterday = wishlists.filter(created__range = (date_today()-timedelta(days=1), date_today()))
        else:
            wishlists.yesterday = wishlists.month.filter(created__day = date_today().day-1)
        return {
            'users': users, 
            'works': works,
            'ebooks': ebooks,
            'wishlists': wishlists,
        }


class FAQView(TemplateView):
    template_name = "faq.html"
    def get_context_data(self, **kwargs):
        location = self.kwargs["location"]
        sublocation = self.kwargs["sublocation"]
        return {'location': location, 'sublocation': sublocation}

class GoodreadsDisplayView(TemplateView):
    template_name = "goodreads_display.html"
    def get_context_data(self, **kwargs):
        context = super(GoodreadsDisplayView, self).get_context_data(**kwargs)
        session = self.request.session
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        
        user = self.request.user
        if user.is_authenticated():
            api_key = ApiKey.objects.filter(user=user)[0].key
            context['api_key'] = api_key

        if user.profile.goodreads_user_id is None:   
            # calculate the Goodreads authorization URL
            (context["goodreads_auth_url"], request_token) = gr_client.begin_authorization(self.request.build_absolute_uri(reverse('goodreads_cb')))
            logger.info("goodreads_auth_url: %s" %(context["goodreads_auth_url"]))
            # store request token in session so that we can redeem it for auth_token if authorization works
            session['goodreads_request_token'] = request_token['oauth_token']
            session['goodreads_request_secret'] = request_token['oauth_token_secret']
        else:
            gr_shelves = gr_client.shelves_list(user_id=user.profile.goodreads_user_id)
            context["shelves_info"] = gr_shelves
            gr_shelf_load_form = GoodreadsShelfLoadingForm()
            # load the shelves into the form
            choices = [('all:%d' % (gr_shelves["total_book_count"]),'all (%d)' % (gr_shelves["total_book_count"]))] +  \
                [("%s:%d" % (s["name"], s["book_count"]) ,"%s (%d)" % (s["name"],s["book_count"])) for s in gr_shelves["user_shelves"]]
            gr_shelf_load_form.fields['goodreads_shelf_name_number'].widget = Select(choices=tuple(choices))
            
            context["gr_shelf_load_form"] = gr_shelf_load_form
            
# also load any CeleryTasks associated with the user
            context["celerytasks"] = models.CeleryTask.objects.filter(user=user)
            
        return context

@login_required
def goodreads_auth(request):

    # calculate the Goodreads authorization URL
    gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
    (goodreads_auth_url, request_token) = gr_client.begin_authorization(request.build_absolute_uri(reverse('goodreads_cb')))
    logger.info("goodreads_auth_url: %s" %(goodreads_auth_url))
    # store request token in session so that we can redeem it for auth_token if authorization works
    request.session['goodreads_request_token'] = request_token['oauth_token']
    request.session['goodreads_request_secret'] = request_token['oauth_token_secret']
    
    return HttpResponseRedirect(goodreads_auth_url)

@login_required    
def goodreads_cb(request):
    """handle callback from Goodreads"""
    
    session = request.session
    authorized_flag = request.GET['authorize']  # is it '1'?
    request_oauth_token = request.GET['oauth_token']

    if authorized_flag == '1':
        request_token = {'oauth_token': session.get('goodreads_request_token'),
                         'oauth_token_secret': session.get('goodreads_request_secret')}
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        
        access_token = gr_client.complete_authorization(request_token)
        
        # store the access token in the user profile
        profile = request.user.profile
        profile.goodreads_auth_token = access_token["oauth_token"]
        profile.goodreads_auth_secret = access_token["oauth_token_secret"]
    
        # let's get the userid, username
        user = gr_client.auth_user()
        
        profile.goodreads_user_id = user["userid"]
        profile.goodreads_user_name = user["name"]
        profile.goodreads_user_link = user["link"]
        
        profile.save()  # is this needed?

    # redirect to the Goodreads display page -- should observe some next later
    return HttpResponseRedirect(reverse('home'))

@require_POST
@login_required
@csrf_exempt    
def goodreads_flush_assoc(request):
    user = request.user
    if user.is_authenticated():
        profile = user.profile
        profile.goodreads_user_id = None
        profile.goodreads_user_name = None
        profile.goodreads_user_link = None
        profile.goodreads_auth_token = None
        profile.goodreads_auth_secret = None
        profile.save()
        logger.info('Goodreads association flushed for user %s', user)
    return HttpResponseRedirect(reverse('goodreads_display'))
      
@require_POST
@login_required      
@csrf_exempt
def goodreads_load_shelf(request):
    """
    a view to allow user load goodreads shelf into her wishlist
    """
    # Should be moved to the API
    goodreads_shelf_name_number = request.POST.get('goodreads_shelf_name_number', 'all:0')
    user = request.user
    try:
        # parse out shelf name and expected number of books
        (shelf_name, expected_number_of_books) = re.match(r'^(.*):(\d+)$', goodreads_shelf_name_number).groups()
        expected_number_of_books = int(expected_number_of_books)
        logger.info('Adding task to load shelf %s to user %s with %d books', shelf_name, user, expected_number_of_books)
        load_task_name = "load_goodreads_shelf_into_wishlist"
        load_task = getattr(tasks, load_task_name)
        task_id = load_task.delay(user.id, shelf_name, expected_number_of_books=expected_number_of_books)
        
        ct = models.CeleryTask()
        ct.task_id = task_id
        ct.function_name = load_task_name
        ct.user = user
        ct.description = "Loading Goodread shelf %s to user %s with %s books" % (shelf_name, user, expected_number_of_books)
        ct.save()
        
        return HttpResponse("<span style='margin: auto 10px auto 36px;vertical-align: middle;display: inline-block;'>We're on it! <a href='JavaScript:window.location.reload()'>Reload the page</a> to see the books we've snagged so far.</span>")
    except Exception,e:
        return HttpResponse("Error in loading shelf: %s " % (e))
        logger.info("Error in loading shelf for user %s: %s ", user, e)


@login_required
def goodreads_calc_shelves(request):

    # we should move towards calculating this only if needed (perhaps with Ajax), caching previous results, etc to speed up
    # performance
    
    if request.user.profile.goodreads_user_id is not None:
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        goodreads_shelves = gr_client.shelves_list(user_id=request.user.profile.goodreads_user_id)
        #goodreads_shelf_load_form = GoodreadsShelfLoadingForm()
        ## load the shelves into the form
        #choices = [('all:%d' % (goodreads_shelves["total_book_count"]),'all (%d)' % (goodreads_shelves["total_book_count"]))] +  \
        #    [("%s:%d" % (s["name"], s["book_count"]) ,"%s (%d)" % (s["name"],s["book_count"])) for s in goodreads_shelves["user_shelves"]]
        #goodreads_shelf_load_form.fields['goodreads_shelf_name_number'].widget = Select(choices=tuple(choices))
    else:
        goodreads_shelf_load_form = None
    
    return HttpResponse(json.dumps(goodreads_shelves), content_type="application/json")
    

@require_POST
@login_required      
@csrf_exempt
def librarything_load(request):
    """
    a view to allow user load librarything library into her wishlist
    """
    # Should be moved to the API
    user = request.user

    
    try:        
        # figure out expected_number_of_books later
        
        lt_username = request.user.profile.librarything_id
        logger.info('Adding task to load librarything %s to user %s', lt_username, user )
        load_task_name = "load_librarything_into_wishlist"
        load_task = getattr(tasks, load_task_name)
        task_id = load_task.delay(user.id, lt_username, None)
        
        ct = models.CeleryTask()
        ct.task_id = task_id
        ct.function_name = load_task_name
        ct.user = user
        ct.description = "Loading LibraryThing collection of %s to user %s." % (lt_username, user)
        ct.save()
            
        return HttpResponse("<span style='margin: auto 10px auto 36px;vertical-align: middle;display: inline-block;'>We're on it! <a href='JavaScript:window.location.reload()'>Reload the page</a> to see the books we've snagged so far.</span>")
    except Exception,e:
        return HttpResponse("Error in loading LibraryThing library: %s " % (e))
        logger.info("Error in loading LibraryThing for user %s: %s ", user, e)

@require_POST
@login_required      
@csrf_exempt
def clear_wishlist(request):
    try:
        request.user.wishlist.works.clear()
        logger.info("Wishlist for user %s cleared", request.user)
        return HttpResponse('wishlist cleared')
    except Exception, e:
        return HttpResponse("Error in clearing wishlist: %s " % (e))
        logger.info("Error in clearing wishlist for user %s: %s ", request.user, e)
    

class LibraryThingView(FormView):
    template_name="librarything.html"
    form_class = LibraryThingForm
    
    def get_context_data(self, **kwargs):
        context = super(LibraryThingView, self).get_context_data(**kwargs)
        form = kwargs['form']
        # get the books for the lt_username in the form
        lt_username=self.request.GET.get("lt_username",None)
        if lt_username is not None:
            lt = librarything.LibraryThing(username=lt_username)
            context.update({'books':list(lt.parse_user_catalog(view_style=5))})
        else:
            context.update({'books':None})
            
        # try picking up the LibraryThing api key -- and set to None if not available.  Not being used for
        # anything crucial at this moment, so a None is ok here
        try:
            context.update({'lt_api_key':settings.LIBRARYTHING_API_KEY})
        except:
            pass
        
        return context

    def form_valid(self,form):
        return super(LibraryThingView, self).form_valid(form)
    
@require_POST
@login_required      
@csrf_exempt
def clear_celery_tasks(request):
    try:
        request.user.tasks.clear()
        logger.info("Celery tasks for user %s cleared", request.user)
        return HttpResponse('Celery Tasks List cleared')
    except Exception, e:
        return HttpResponse("Error in clearing Celery Tasks: %s " % (e))
        logger.info("Error in clearing Celery Tasks for user %s: %s ", request.user, e)    

def celery_test(request):
    return HttpResponse("celery_test")

# routing views that try to redirect to the works page on a 3rd party site
#
# TODO: need to queue up a task to look up IDs if we have to fallback to 
# routing based on ISBN or search

def work_librarything(request, work_id):
    work = get_object_or_404(models.Work, id=work_id)
    isbn = work.first_isbn_13()
    if work.librarything_id:
        url = work.librarything_url
    elif isbn:
        # TODO: do the redirect here and capture the work id?
        url = "http://www.librarything.com/isbn/%s" % isbn
    else:
        term = work.title + " " + work.author()
        q = urlencode({'searchtpe': 'work', 'term': term})
        url = "http://www.librarything.com/search.php?" + q
    return HttpResponseRedirect(url)

def work_openlibrary(request, work_id):
    work = get_object_or_404(models.Work, id=work_id)
    isbns = ["ISBN:" + i.value for i in work.identifiers.filter(type='isbn')]
    url = None

    if work.openlibrary_id:
        url = work.openlibrary_url
    elif len(isbns) > 0:
        isbns = ",".join(isbns)
        u = 'http://openlibrary.org/api/books?bibkeys=%s&jscmd=data&format=json' % isbns
        try:
            j = json.loads(requests.get(u).content)
            # as long as there were some matches get the first one and route to it
            if len(j.keys()) > 0:
                first = j.keys()[0]
                url = "http://openlibrary.org" + j[first]['key'] 
        except ValueError:
            # fail at openlibrary
            logger.warning("failed to get OpenLibrary json at %s" % u)          
    # fall back to doing a search on openlibrary
    if not url:
        q = urlencode({'q': work.title + " " + work.author()})
        url = "http://openlibrary.org/search?" + q
    return HttpResponseRedirect(url)

def work_goodreads(request, work_id):
    work = get_object_or_404(models.Work, id=work_id)
    isbn = work.first_isbn_13()
    if work.goodreads_id:
        url = work.goodreads_url
    elif isbn:
        url = "http://www.goodreads.com/book/isbn/%s" % isbn
    else:
        q = urlencode({'query': work.title + " " + work.author()})
        url = "http://www.goodreads.com/search?" + q
    return HttpResponseRedirect(url)

@login_required
def emailshare(request):
    if request.method == 'POST':
        form=EmailShareForm(request.POST)
        if form.is_valid():
            subject = form.cleaned_data['subject']
            message = form.cleaned_data['message']
            sender = form.cleaned_data['sender']
            recipient = form.cleaned_data['recipient']
            send_mail_task.delay(subject, message, sender, [recipient])
            try:
                next = form.cleaned_data['next']
            except:
                next = ''
            return HttpResponseRedirect(next)
            
    else:
    	sender = request.user.email
        try:
            next = request.GET['next']
            if "pledge" in request.path:
                work_id = next.split('=')[1]
                book = models.Work.objects.get(pk=int(work_id))
                title = book.title
                message = "I just pledged to unglue one of my favorite books, "+title+", on Unglue.It: http://unglue.it/work/"+work_id+".  If enough of us pledge to unglue this book, the creator will be paid and the ebook will become free to everyone on earth.  Will you join me?"
                subject = "Help me unglue "+title
            else:
                work_id = next.split('/')[-2]
                work_id = int(work_id)
                book = models.Work.objects.get(pk=work_id)
                title = book.title
                # if title requires unicode let's ignore it for now
                try:
                    title = ', '+str(title)+', '
                except:
                    title = ' '
                try:
                    status = book.last_campaign().status
                except:
                    status = None
            
                # customize the call to action depending on campaign status
                if status == 'ACTIVE':
                    message = 'Help me unglue one of my favorite books'+title+'on Unglue.It: http://unglue.it/'+next+'. If enough of us pledge to unglue this book, the creator will be paid and the ebook will become free to everyone on earth.'
                else:
                    message = 'Help me unglue one of my favorite books'+title+'on Unglue.It: http://unglue.it'+next+'. If enough of us wishlist this book, Unglue.It may start a campaign to pay the creator and make the ebook free to everyone on earth.' 
                subject = 'Come see one of my favorite books on Unglue.It'
            
            form = EmailShareForm(initial={'sender': sender, 'next':next, 'subject': subject, 'message': message})
        except:
            next = ''
            form = EmailShareForm(initial={'sender': sender, 'next':next, 'subject': 'Come join me on Unglue.It', 'message':"I'm ungluing books on Unglue.It.  Together we're paying creators and making ebooks free to everyone on earth.  Join me! http://unglue.it"})

    return render(request, "emailshare.html", {'form':form})    
    
def feedback(request):
    num1 = randint(0,10)
    num2 = randint(0,10)
    sum = num1 + num2
    
    if request.method == 'POST':
        form=FeedbackForm(request.POST)
        if form.is_valid():
            subject = form.cleaned_data['subject']
            message = form.cleaned_data['message']
            sender = form.cleaned_data['sender']
            recipient = 'support@gluejar.com'
            page = form.cleaned_data['page']
            useragent = request.META['HTTP_USER_AGENT']
            if request.user.is_anonymous():
                ungluer = "(not logged in)"
            else:
                ungluer = request.user.username
            message = "<<<This feedback is about "+page+". Original user message follows\nfrom "+sender+", ungluer name "+ungluer+"\nwith user agent "+useragent+"\n>>>\n"+message
            send_mail_task.delay(subject, message, sender, [recipient])
            
            return render(request, "thanks.html", {"page":page}) 
            
        else:
            num1 = request.POST['num1']
            num2 = request.POST['num2']
        
    else:
        if request.user.is_authenticated():
            sender=request.user.email;
        else:
            sender=''
        try:
            page = request.GET['page']
        except:
            page='/'
        form = FeedbackForm(initial={"sender":sender, "subject": "Feedback on page "+page, "page":page, "num1":num1, "num2":num2, "answer":sum})
        
    return render(request, "feedback.html", {'form':form, 'num1':num1, 'num2':num2})    
        
def comment(request):
    latest_comments = Comment.objects.all().order_by('-submit_date')[:20]
    return render(request, "comments.html", {'latest_comments': latest_comments})

def campaign_archive_js(request):
    """ proxy for mailchimp js"""
    response = HttpResponse()
    r = requests.get(settings.CAMPAIGN_ARCHIVE_JS)
    response.status_code = r.status_code
    response.content = r.content
    response["Content-Type"] = "text/javascript"
    return response
